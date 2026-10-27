# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex work-ledger``: serve on the ledger host, send rows (OMN-20275), or request a PR handoff (OMN-20636)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import re
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
    bound_lane_principal,
    open_lab_run_bus,
)
from omnimarket.events.topics import (
    PR_HANDOFF_ACCEPTED_TOPIC_V1,
    PR_HANDOFF_FAILED_TOPIC_V1,
    PR_HANDOFF_HANDED_OFF_TOPIC_V1,
    PR_HANDOFF_REQUESTED_TOPIC_V1,
)
from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.select import (
    default_state_dir,
    open_bound_bus,
    positional_opener,
    replay_outbox,
)
from omnimarket.handlers.work_ledger_consume.consume_watch import (
    ConsumeWatch,
    load_consume_watch_policy,
    readiness_probe,
)
from omnimarket.lab_work.bus import _bytes, _subscribe
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.bus_binding import EnumBusBinding
from omnimarket.models.pr_handoff import (
    ModelPrHandoffAccepted,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffRequested,
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
    load_work_ledger_append_topics,
)


@click.group("work-ledger")
@click.pass_context
def work_ledger_group(ctx: click.Context) -> None:
    """Append exact lab-host rows to the ledger host over the declared bus."""
    ctx.ensure_object(dict)


def _writer_principals(
    ctx: click.Context, param: click.Parameter, values: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        principal
        for value in values
        for principal in re.split(r"[,\s]+", value)
        if principal
    )


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
    "--writer-principal",
    multiple=True,
    envvar="ONEX_WORK_LEDGER_WRITER_PRINCIPALS",
    callback=_writer_principals,
    help="Broker writer principal whose topic this host serves; may be repeated.",
)
@click.option(
    "--operator-principal",
    envvar="ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL",
    help="Broker principal allowed to write operator rows and attributed to mirrored terminals.",
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
    writer_principal: tuple[str, ...],
    operator_principal: str | None,
    operator_capture: bool,
) -> None:
    """Serve commands using the local append command on the ledger host."""
    try:
        argv = shlex.split(append_command)
    except ValueError as exc:
        raise click.UsageError(f"--append-command: {exc}") from exc
    if not argv:
        raise click.UsageError("--append-command must not be empty")
    if not writer_principal:
        raise click.UsageError(
            "serve requires at least one --writer-principal (ONEX_WORK_LEDGER_WRITER_PRINCIPALS)"
        )
    topics = load_work_ledger_append_topics()
    try:
        for principal in writer_principal:
            topics.writer_command_topic(principal)
        if operator_principal is not None:
            topics.writer_command_topic(operator_principal)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
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
        writer_principals=writer_principal,
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
                writer_principals=writer_principal,
                operator_principal=operator_principal,
            )
            await host.start()
            # OMN-20275: retake the subscriptions when the broker drops the group; degraded and alarmed.
            watch = ConsumeWatch(
                host,
                readiness_probe(opened, lambda: host.serving_topics),
                load_consume_watch_policy(),
                host_name=host_name,
                state_dir=default_state_dir(),
            )
            watch.start()
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
                await watch.stop()
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
@click.option("--principal", help="Writer principal for the in-memory bus only.")
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
) -> None:
    """Send exact rows, print one JSON receipt and exit with the outcome."""
    if bus == "kafka" and principal is not None:
        raise click.UsageError(
            "the broker login is the writer identity; --principal is for the in-memory bus only"
        )
    if bus == "inmemory" and principal is None:
        raise click.UsageError("append with the in-memory bus requires --principal")
    if principal is not None:
        try:
            load_work_ledger_append_topics().writer_command_topic(principal)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    try:
        with click.open_file(rows_file, "r", encoding="utf-8") as source:
            rows = source.read()
        request = ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=rows,
            requested_by_lane=lane,
            requesting_host=host,
            requested_at=datetime.now(UTC),
        )
    except (OSError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc

    state_dir = default_state_dir()

    async def main() -> int:
        # OMN-20275: the lane overlay orders the bindings. A durable bus that fails its health read puts this
        # append in the local outbox (the in-memory binding has no ledger host), where the next healthy
        # command replays it under the same request id.
        async with open_bound_bus(
            open_bus=positional_opener(open_lab_run_bus),
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            workspace_root=omnibase_path,
            state_dir=state_dir,
            pending_outbox=lambda: outbox.pending_count(state_dir),
        ) as bound:
            if bound.decision.degraded:
                queued = outbox.queue_request(state_dir, request)
                click.echo(
                    json.dumps(
                        {
                            "request_id": str(request_id),
                            "status": "queued",
                            "exit_code": 0,
                            "binding": bound.decision.binding.value,
                            "bus_state": bound.decision.state.value,
                            "outbox": queued.name,
                            "message": "durable bus unavailable; queued for replay under this request id",
                        },
                        sort_keys=True,
                    )
                )
                return 0
            caller = WorkLedgerAppendCaller(
                bound.bus,
                principal=principal
                if principal is not None
                else bound_lane_principal(),
            )
            try:
                if bound.decision.binding is EnumBusBinding.DURABLE:
                    replay = await replay_outbox(
                        bound,
                        state_dir,
                        lambda queued: caller.append(queued, timeout_s=timeout_s),
                    )
                    if replay.replayed or replay.duplicates or replay.refused:
                        click.echo(
                            f"bus-binding outbox replayed={replay.replayed} duplicates={replay.duplicates} "
                            f"refused={replay.refused} remaining={replay.remaining}",
                            err=True,
                        )
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
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
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
