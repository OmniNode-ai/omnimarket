# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex lab-work``: serve lab work units on a pool host, list the pool's
capacity, and place and send one unit over the bus (OMN-20105).

    onex lab-work serve --host-name h202 --kafka-bootstrap <broker>
    onex lab-work hosts --kafka-bootstrap <broker>
    onex lab-work run --repo OmniNode-ai/omnimarket --sha <pushed sha> \
        --lane <lane> --kafka-bootstrap <broker> -- uv run pytest tests/unit -q

``run`` exits with the command's own exit code when it ran, 75 when every
advertising pool host is over the bar (or lacks a needed tool), 69 when no pool
host advertised at all, and 70 when the unit was refused or could not be run. It never runs the command on
the calling host unless that host is a serving pool member placed like any
other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import cast

import click

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.lab_work.bus import (
    DEFAULT_MAX_COMMAND_AGE_SECONDS,
    LabWorkCaller,
    LabWorkHost,
)
from omnimarket.lab_work.placement import (
    DEFAULT_MAX_LOAD_PER_CORE,
    EnumPlacementDecision,
    EnumRefusalReason,
    newest_per_host,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    DEFAULT_ALLOWED_EXECUTABLES,
    DEFAULT_ALLOWED_OWNERS,
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import WorkKind

EXIT_NO_HOST = 75
#: No pool host advertised at all: nothing could be checked. Kept apart from 75 (every host over the
#: bar) because the instruction differs: a serve process is down, or the interim ssh path is needed.
EXIT_NO_ADVERTISEMENT = 69
EXIT_NOT_RUN = 70
#: ``serve`` found a worker that held one unit for two of its limits while the advertiser was
#: still beating. Non-zero so the service manager restarts the host and a stall is not silent.
EXIT_STUCK_WORKER = 71


def _exit_hard(code: int) -> None:
    """Leave now. A worker thread that never returns would hold a normal interpreter
    exit open (the asyncio executor and ``threading`` both join it), so a stuck host
    flushes its log and ends the process without that wait."""
    logging.shutdown()
    os._exit(code)


_bus_options = [
    click.option(
        "--omnibase-path",
        envvar="OMNIBASE_PATH",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Workspace root holding the lane declaration. Bound to $OMNIBASE_PATH.",
    ),
    click.option(
        "--bus",
        type=click.Choice(["kafka", "inmemory"]),
        default="kafka",
        show_default=True,
    ),
    click.option("--bus-lane", default=None, help="The declared bus lane to use."),
    click.option(
        "--kafka-bootstrap", default=None, help="The broker, stated directly."
    ),
]


def _with_bus_options(func: click.decorators.FC) -> click.decorators.FC:
    for option in reversed(_bus_options):
        func = option(func)
    return func


@click.group("lab-work")
@click.pass_context
def lab_work_group(ctx: click.Context) -> None:
    """Place heavy work on the pool host with the most free capacity, over the bus."""
    ctx.ensure_object(dict)


@lab_work_group.command("serve")
@_with_bus_options
@click.option(
    "--host-name", required=True, help="This host's pool name (a deployment fact)."
)
@click.option("--max-units", type=int, default=1, show_default=True)
@click.option(
    "--rank-penalty",
    type=float,
    default=0.0,
    show_default=True,
    help="Added to this host's load per core for ranking only (an evidence-lane host).",
)
@click.option(
    "--allowed-owner",
    "allowed_owners",
    multiple=True,
    default=sorted(DEFAULT_ALLOWED_OWNERS),
)
@click.option(
    "--allowed-executable",
    "allowed_executables",
    multiple=True,
    default=sorted(DEFAULT_ALLOWED_EXECUTABLES),
)
@click.option("--max-command-age", type=int, default=DEFAULT_MAX_COMMAND_AGE_SECONDS)
@click.option(
    "--max-commands", type=int, default=0, help="Stop after this many units (0: serve)."
)
@click.option(
    "--work-root", type=click.Path(file_okay=False, path_type=Path), default=None
)
def serve_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
    max_units: int,
    rank_penalty: float,
    allowed_owners: tuple[str, ...],
    allowed_executables: tuple[str, ...],
    max_command_age: int,
    max_commands: int,
    work_root: Path | None,
) -> None:
    """Serve lab work units addressed to this host, and advertise its capacity."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
        LocalShellLabWorkExecutor,
    )

    work = HandlerLabWorkUnitEffect(
        host_name,
        LocalShellLabWorkExecutor(work_root),
        allowed_owners=frozenset(allowed_owners),
        allowed_executables=frozenset(allowed_executables),
    )

    async def main() -> int:
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
            host = LabWorkHost(
                opened,
                work,
                HandlerHostCapacityAdvertiseEffect(),
                max_units=max_units,
                rank_penalty=rank_penalty,
                max_command_age_seconds=max_command_age,
            )
            code = 0
            await host.start()
            try:
                while not stop.is_set():
                    if host.stuck.is_set():
                        click.echo(
                            f"lab-work: stuck worker: {host.stuck_reason}", err=True
                        )
                        code = EXIT_STUCK_WORKER
                        break
                    if max_commands and host.processed >= max_commands:
                        break
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
            finally:
                await host.stop()
        if code:
            _exit_hard(code)
        return code

    try:
        code = asyncio.run(main())
    except LabRunBusError as exc:
        raise click.ClickException(f"bus: {exc}") from exc
    if code:
        sys.exit(code)


@lab_work_group.command("hosts")
@_with_bus_options
@click.option(
    "--window", type=float, default=0.0, help="Seconds to listen (default 2 cadences)."
)
@click.option("--json", "as_json", is_flag=True)
def hosts_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    window: float,
    as_json: bool,
) -> None:
    """Print the newest capacity advertisement of every serving pool host."""

    async def main() -> None:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            caller = LabWorkCaller(opened)
            await caller.start()
            try:
                ads = await caller.collect(
                    window or 2 * caller.topics.cadence_seconds + 2
                )
            finally:
                await caller.stop()
        newest = newest_per_host(ads)
        if as_json:
            click.echo(
                json.dumps([ad.model_dump(mode="json") for ad in newest], indent=2)
            )
            return
        if not newest:
            click.echo("no pool host advertised in the window", err=True)
        for ad in newest:
            click.echo(
                f"{ad.host_name}: load/core={ad.load_per_core:.2f} (load1={ad.load1:.1f}"
                f"+{ad.running_units} running on {ad.cores} cores) "
                f"free={ad.mem_available_bytes / 1024**3:.1f}GB tools={','.join(ad.tools)} "
                f"at {ad.advertised_at:%H:%M:%SZ}"
            )

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        raise click.ClickException(f"bus: {exc}") from exc


@lab_work_group.command("run", context_settings={"ignore_unknown_options": True})
@_with_bus_options
@click.option("--repo", required=True, help="owner/name on GitHub.")
@click.option("--sha", required=True, help="A full, PUSHED commit sha.")
@click.option("--lane", required=True, help="The lane sending the unit (attribution).")
@click.option(
    "--kind",
    type=click.Choice(["test", "build", "lint", "code_draft", "other"]),
    default="other",
    show_default=True,
)
@click.option("--timeout", "timeout_seconds", type=int, default=1800, show_default=True)
@click.option("--need", "need_tools", multiple=True, help="A tool the host must have.")
@click.option(
    "--host", "only_host", default=None, help="Only this pool host (bar still applies)."
)
@click.option("--max-load-per-core", type=float, default=DEFAULT_MAX_LOAD_PER_CORE)
@click.option("--min-free-gb", type=float, default=4.0)
@click.option(
    "--window", type=float, default=0.0, help="Seconds to listen for advertisements."
)
@click.option("--dry-run", is_flag=True, help="Print the placement and send nothing.")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
def run_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    repo: str,
    sha: str,
    lane: str,
    kind: str,
    timeout_seconds: int,
    need_tools: tuple[str, ...],
    only_host: str | None,
    max_load_per_core: float,
    min_free_gb: float,
    window: float,
    dry_run: bool,
    argv: tuple[str, ...],
) -> None:
    """Place one heavy command on the pool host with the most free capacity,
    send it over the bus, and wait for its receipt."""
    words = list(argv[1:] if argv and argv[0] == "--" else argv)
    if not words:
        raise click.UsageError("no command after --")
    if "/" not in repo:
        raise click.UsageError("--repo must be owner/name")

    async def main() -> int:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            caller = LabWorkCaller(opened)
            await caller.start()
            try:
                await caller.collect(window or 2 * caller.topics.cadence_seconds + 2)
                placement = caller.place(
                    max_load_per_core=max_load_per_core,
                    min_free_bytes=int(min_free_gb * 1024**3),
                    need_tools=need_tools,
                    only_host=only_host,
                )
                click.echo(placement.describe(), err=True)
                if placement.decision is EnumPlacementDecision.REFUSED:
                    click.echo(
                        "lab-work: no pool host can take this unit; not running it anywhere",
                        err=True,
                    )
                    if placement.refusal is EnumRefusalReason.COULD_NOT_CHECK:
                        return EXIT_NO_ADVERTISEMENT
                    return EXIT_NO_HOST
                if dry_run:
                    return 0
                request = ModelLabWorkUnitRequest(
                    work_unit_id=f"lw-{uuid.uuid4().hex[:16]}",
                    target_host=placement.host_name,
                    repo=repo,
                    commit_sha=sha,
                    argv=words,
                    kind=cast(WorkKind, kind),
                    timeout_seconds=timeout_seconds,
                    lane=lane,
                    need_tools=list(need_tools),
                )
                click.echo(
                    f"lab-work: sent {request.work_unit_id} to {request.target_host}",
                    err=True,
                )
                receipt = await caller.dispatch(request)
            finally:
                await caller.stop()
        if receipt.output_tail:
            click.echo(receipt.output_tail)
        click.echo(
            json.dumps(receipt.model_dump(mode="json", exclude={"output_tail"})),
            err=True,
        )
        if (
            receipt.status is EnumLabWorkUnitStatus.COMPLETED
            and receipt.exit_code is not None
        ):
            return receipt.exit_code
        return EXIT_NOT_RUN

    try:
        code = asyncio.run(main())
    except LabRunBusError as exc:
        raise click.ClickException(f"bus: {exc}") from exc
    sys.exit(code)


def main() -> None:
    """``python -m omnimarket.lab_work.cli``: the same group without the onex CLI."""
    lab_work_group()


if __name__ == "__main__":
    main()


__all__ = [
    "EXIT_NOT_RUN",
    "EXIT_NO_ADVERTISEMENT",
    "EXIT_NO_HOST",
    "EXIT_STUCK_WORKER",
    "hosts_command",
    "lab_work_group",
    "run_command",
    "serve_command",
]
