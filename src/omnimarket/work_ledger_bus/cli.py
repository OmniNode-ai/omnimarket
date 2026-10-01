# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex work-ledger``: serve on the ledger host or send rows (OMN-20275)."""

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
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller, WorkLedgerAppendHost


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
def serve_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
    ledger: Path,
    append_command: str,
) -> None:
    """Serve commands using the local append command on the ledger host."""
    try:
        argv = shlex.split(append_command)
    except ValueError as exc:
        raise click.UsageError(f"--append-command: {exc}") from exc
    if not argv:
        raise click.UsageError("--append-command must not be empty")
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
    handler = HandlerWorkLedgerAppendEffect(
        LocalLedgerAppendCommand(argv), LocalLedgerFile(ledger), host_name
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
            host = WorkLedgerAppendHost(opened, handler)
            await host.start()
            try:
                await stop.wait()
            finally:
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
) -> None:
    """Send exact rows, print one JSON receipt and exit with the outcome."""
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


def main() -> None:
    """The same group without the onex CLI."""
    work_ledger_group()


if __name__ == "__main__":
    main()
