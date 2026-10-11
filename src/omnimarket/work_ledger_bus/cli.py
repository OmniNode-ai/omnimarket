# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex work-ledger``: serve on the ledger host, send rows (OMN-20275), or request a PR handoff (OMN-20636)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import shlex
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import click
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.events.topics import (
    PR_HANDOFF_ACCEPTED_TOPIC_V1,
    PR_HANDOFF_FAILED_TOPIC_V1,
    PR_HANDOFF_HANDED_OFF_TOPIC_V1,
    PR_HANDOFF_REQUESTED_TOPIC_V1,
)
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.pr_handoff import (
    ModelPrHandoffAccepted,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffRequested,
)
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerPrincipalRecords,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_serve import (
    ledger_host_capture,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendCaller,
    WorkLedgerAppendHost,
    load_work_ledger_signing_key,
)


@click.group("work-ledger")
@click.pass_context
def work_ledger_group(ctx: click.Context) -> None:
    """Append exact lab-host rows to the ledger host over the declared bus."""
    ctx.ensure_object(dict)


@work_ledger_group.command("serve")
@_with_bus_options
@click.option("--host-name", required=True)
@click.option(
    "--ledger", required=True, type=click.Path(dir_okay=False, path_type=Path)
)
@click.option(
    "--append-command",
    required=True,
    help="Shell-quoted argv prefix, including the ledger path.",
)
@click.option(
    "--principal-records",
    envvar="ONEX_WORK_LEDGER_PRINCIPAL_RECORDS",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Trusted issuer-owned JSON containing principal -> base64 Ed25519 public key records.",
)
@click.option(
    "--operator-principal",
    envvar="ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL",
    help="Operator identity in the issuer records, supplied by the ledger host.",
)
@click.option(
    "--signing-key-file",
    envvar="ONEX_WORK_LEDGER_SIGNING_KEY_FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Operator Ed25519 private key in PEM form; signs mirrored lab terminals.",
)
@click.option(
    "--operator-capture/--no-operator-capture",
    envvar="ONEX_WORK_LEDGER_OPERATOR_CAPTURE",
    default=False,
    show_default=True,
    help=(
        "Also record the operator's decisions and asks, taken from the content-capture "
        "topic, as rows on this ledger (OMN-20905)."
    ),
)
def serve_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
    ledger: Path,
    append_command: str,
    principal_records: Path | None,
    operator_principal: str | None,
    signing_key_file: Path | None,
    operator_capture: bool,
) -> None:
    """Serve commands using the local append command on the ledger host."""
    try:
        argv = shlex.split(append_command)
    except ValueError as exc:
        raise click.UsageError(f"--append-command: {exc}") from exc
    if not argv:
        raise click.UsageError("--append-command must not be empty")
    if principal_records is None or operator_principal is None:
        raise click.UsageError(
            "serve requires --principal-records and --operator-principal "
            "(ONEX_WORK_LEDGER_PRINCIPAL_RECORDS, ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL)"
        )
    try:
        records = ModelWorkLedgerPrincipalRecords.model_validate_json(
            principal_records.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            "cannot read valid issuer principal records"
        ) from exc
    public_keys = records.verification_keys()
    if operator_principal not in public_keys:
        raise click.UsageError(
            "--operator-principal must have an issuer public key record"
        )
    mirror_key = None
    if signing_key_file is not None:
        try:
            mirror_key = load_work_ledger_signing_key(signing_key_file)
        except (OSError, ValueError, TypeError) as exc:
            raise click.ClickException("cannot read an Ed25519 signing key") from exc
        if mirror_key.public_key() != public_keys[operator_principal]:
            raise click.UsageError(
                "--signing-key-file does not match the operator principal's public key"
            )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # One serve process per ledger file: a second one (or an old one still
    # finishing after a rebalance) would let two check-then-append steps overlap
    # (model review, omnibase_internal tla/ledger_bus_append/REVIEW.md).
    lock_path = ledger.with_name(ledger.name + ".work-ledger-serve.lock")
    try:
        lock_file = lock_path.open("a+")
    except OSError as exc:
        click.echo(f"serve lock {lock_path}: {exc}", err=True)
        sys.exit(73)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        click.echo(
            f"another work-ledger serve process holds {lock_path}; refusing to start",
            err=True,
        )
        sys.exit(75)
    append_runner = LocalLedgerAppendCommand(argv)
    handler = HandlerWorkLedgerAppendEffect(
        append_runner,
        LocalLedgerFile(ledger),
        host_name,
        public_keys=public_keys,
        operator_principal=operator_principal,
    )

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            host = WorkLedgerAppendHost(
                opened,
                handler,
                mirror_principal=operator_principal if mirror_key else None,
                mirror_signing_key=mirror_key,
            )
            await host.start()
            # OMN-20905 (RULING 2026-10-10T22:40:43Z): the operator capture runs beside the
            # append host, off the same bus, through the same append runner.
            capture = (
                ledger_host_capture(
                    opened,
                    ledger=ledger,
                    append_runner=append_runner,
                    host_name=host_name,
                    bus_lane=bus_lane,
                )
                if operator_capture
                else None
            )
            if capture is not None:
                await capture.start()
            try:
                await stop.wait()
            finally:
                if capture is not None:
                    await capture.stop()
                await host.stop()

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(69)


@work_ledger_group.command("append")
@_with_bus_options
@click.option("--lane", required=True, help="Requesting lane (attribution).")
@click.option("--host", required=True, help="Requesting host (attribution).")
@click.option("--request-id", required=True, type=click.UUID)
@click.option(
    "--rows-file", default="-", type=click.Path(dir_okay=False, allow_dash=True)
)
@click.option(
    "--timeout-s",
    type=click.FloatRange(min=0, min_open=True),
    default=60.0,
    show_default=True,
)
@click.option("--principal", envvar="ONEX_WORK_LEDGER_PRINCIPAL")
@click.option(
    "--signing-key-file",
    envvar="ONEX_WORK_LEDGER_SIGNING_KEY_FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Issuer-provisioned Ed25519 private key in PEM form; never sent over the bus.",
)
def append_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    lane: str,
    host: str,
    request_id: UUID,
    rows_file: str,
    timeout_s: float,
    principal: str | None,
    signing_key_file: Path | None,
) -> None:
    """Send exact rows, print one JSON receipt and exit with the outcome."""
    if principal is None or signing_key_file is None:
        raise click.UsageError(
            "append requires --principal and --signing-key-file "
            "(ONEX_WORK_LEDGER_PRINCIPAL, ONEX_WORK_LEDGER_SIGNING_KEY_FILE)"
        )
    try:
        key = load_work_ledger_signing_key(signing_key_file)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException("cannot read an Ed25519 signing key") from exc
    try:
        with click.open_file(rows_file, "r", encoding="utf-8") as source:
            rows = source.read()
        request = ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=rows,
            requested_by_lane=lane,
            requesting_host=host,
            requested_at=datetime.now(UTC),
        ).signed(principal, key)
    except (OSError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc

    async def main() -> int:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            caller = WorkLedgerAppendCaller(opened)
            try:
                receipt = await caller.append(request, timeout_s=timeout_s)
            except TimeoutError:
                click.echo(
                    json.dumps({"status": "pending", "request_id": str(request_id)})
                )
                return 75
            finally:
                await caller.stop()
        click.echo(receipt.model_dump_json())
        if receipt.status is EnumWorkLedgerAppendStatus.ERROR:
            return 70
        return receipt.exit_code

    try:
        code = asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        code = 69
    sys.exit(code)


# OMN-20636: the answers a handoff request waits for, by the event's class name.
_HANDOFF_ANSWERS: dict[str, type[BaseModel]] = {
    PR_HANDOFF_ACCEPTED_TOPIC_V1: ModelPrHandoffAccepted,
    PR_HANDOFF_HANDED_OFF_TOPIC_V1: ModelPrHandoffHandedOff,
    PR_HANDOFF_FAILED_TOPIC_V1: ModelPrHandoffFailed,
}


@work_ledger_group.command("handoff")
@_with_bus_options
@click.option(
    "--request-file",
    default="-",
    type=click.Path(dir_okay=False, allow_dash=True),
    help="One ModelPrHandoffRequested as JSON.",
)
@click.option(
    "--wait-s",
    type=click.FloatRange(min=0),
    default=30.0,
    show_default=True,
    help="How long to wait for the orchestrator's first answer; 0 publishes only.",
)
@click.option(
    "--settle-s",
    type=click.FloatRange(min=0),
    default=5.0,
    show_default=True,
    help=(
        "After an acceptance, how long to keep listening for a terminal: a refusal "
        "or a handoff decided in the same leg is published right behind it."
    ),
)
def handoff_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    request_file: str,
    wait_s: float,
    settle_s: float,
) -> None:
    """Publish one PR handoff request; print the orchestrator's answer as JSON.

    The answer printed is a terminal (handed off or failed) when one arrives
    within the wait, or within ``--settle-s`` of the acceptance; otherwise the
    acceptance, after which the orchestrator answers on the bus alone.

    The decision is node_pr_handoff_orchestrator's, on the PR watcher's live
    observations; this verb decides nothing. Exit 0 accepted or handed off,
    4 failed (the answer names the error_code), 75 no answer in time (the
    request is published and will be answered on the bus), 69 bus error.
    """
    try:
        with click.open_file(request_file, "r", encoding="utf-8") as source:
            request = ModelPrHandoffRequested.model_validate_json(source.read())
    except (OSError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc

    async def main() -> int:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            answers: asyncio.Queue[tuple[str, BaseModel]] = asyncio.Queue()
            unsubscribes = []
            group = f"pr-handoff-request.{request.correlation_id.hex[:12]}"
            if wait_s > 0:
                for topic, model in _HANDOFF_ANSWERS.items():

                    async def on_message(
                        message: ProtocolBusMessage,
                        topic: str = topic,
                        model: type[BaseModel] = model,
                    ) -> None:
                        try:
                            raw = json.loads(message.value)
                            payload = raw.get("payload", raw)
                            answer = model.model_validate(payload)
                        except (ValueError, AttributeError):
                            return
                        if (
                            getattr(answer, "correlation_id", None)
                            == request.correlation_id
                        ):
                            await answers.put((topic, answer))

                    unsubscribes.append(
                        await _subscribe(opened, topic, on_message, group, "latest")
                    )
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=request.model_dump(mode="json"),
                correlation_id=request.correlation_id,
                event_type=event_type_for(PR_HANDOFF_REQUESTED_TOPIC_V1),
                payload_type=ModelPrHandoffRequested.__name__,
            )
            try:
                await opened.publish(
                    PR_HANDOFF_REQUESTED_TOPIC_V1,
                    request.handoff_key.encode("utf-8"),
                    _bytes(envelope),
                )
                if wait_s == 0:
                    click.echo(
                        json.dumps(
                            {
                                "status": "published",
                                "correlation_id": str(request.correlation_id),
                            }
                        )
                    )
                    return 0
                try:
                    topic, answer = await asyncio.wait_for(
                        answers.get(), timeout=wait_s
                    )
                    if isinstance(answer, ModelPrHandoffAccepted):
                        # The same leg may refuse or hand off right behind the
                        # acceptance; a lane told "accepted" must not miss it.
                        with contextlib.suppress(TimeoutError):
                            topic, answer = await asyncio.wait_for(
                                answers.get(), timeout=settle_s
                            )
                except TimeoutError:
                    click.echo(
                        json.dumps(
                            {
                                "status": "pending",
                                "correlation_id": str(request.correlation_id),
                            }
                        )
                    )
                    return 75
            finally:
                for unsubscribe in unsubscribes:
                    await unsubscribe()
        click.echo(
            json.dumps({"topic": topic, "answer": answer.model_dump(mode="json")})
        )
        return 4 if isinstance(answer, ModelPrHandoffFailed) else 0

    try:
        code = asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        code = 69
    sys.exit(code)


def main() -> None:
    """The same group without the onex CLI."""
    work_ledger_group()


if __name__ == "__main__":
    main()
