# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_reading_effect <verb>``.

``hourly``   read this host's probes and append the rows (what the schedule runs).
``probe``    the same reads, appending nothing.
``join-j2``  run the J2 join (piece 2 to piece 3) for each pooled host on a bus, emit its two chains.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 every probe was read and written (a probe reading NOT WORKING is still 0: a probe
changes nothing and refuses nothing); 1 the run or a row failed (an unreadable source, an append
that did not land, a host the overlay does not declare); 3 a deployment fact is missing
(INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.lab_work.bus import LabWorkCaller
from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostReadingCommand,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import GitRepoSource
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import GraphRules
from omnimarket.nodes.node_code_graph_effect.overlay import (
    load_overlay as load_code_graph_overlay,
)
from omnimarket.nodes.node_host_reading_effect.adapters.canary_route import (
    CommandAdvertisementSource,
    HostTableInventory,
    SubprocessCanaryRoute,
)
from omnimarket.nodes.node_host_reading_effect.adapters.graph_verdict_source import (
    GraphBuildingVerdictSource,
)
from omnimarket.nodes.node_host_reading_effect.adapters.host_files import LocalHostFiles
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_join_j2 import (
    HandlerJoinJ2,
)
from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    HostReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_host_reading_effect.overlay import (
    NODE_NAME,
    ModelHostReadingOverlay,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)

# The opener's keyword for the workspace root, read from its signature rather than spelled here.
_WORKSPACE_KW = next(
    name
    for name in inspect.signature(open_lab_run_bus).parameters
    if name.endswith("_home")
)


def _adverts(overlay: ModelHostReadingOverlay) -> CommandAdvertisementSource | None:
    """The advertisement source the overlay names, or None (not configured, or not set here)."""
    canary = overlay.canary
    if canary is None or canary.advertisements_command_env is None:
        return None
    command = os.environ.get(canary.advertisements_command_env, "").strip()
    if not command:
        return None
    return CommandAdvertisementSource(
        command, canary.advertisements_timeout_seconds, os.environ
    )


def _join_j1_ports(
    overlay: ModelHostReadingOverlay,
) -> tuple[GraphBuildingVerdictSource, JsonlEmissionSink | None]:
    """The graph source and emission journal of join J1, built from the code graph's overlay and this one."""
    settings = overlay.join_j1
    if settings is None:
        raise ValueError("the overlay declares no join_j1 settings")
    graph = load_code_graph_overlay()
    if graph.graph_backend != "git":
        raise ValueError(
            "graph_backend in_memory is the test backend; a deployment reads git"
        )
    root = os.environ.get(graph.repos_root_env, "").strip()
    if not root:
        raise ValueError(
            f"{graph.repos_root_env} is not set; the code graph overlay names it as the clones' root"
        )
    source = GraphBuildingVerdictSource(
        source=GitRepoSource(
            Path(root), max_file_bytes=graph.graph_roots.max_file_bytes
        ),
        rules=GraphRules(roots=graph.graph_roots),
        repos=tuple((repo.name, repo.ref) for repo in graph.repos),
        nodes=settings.nodes,
    )
    if settings.emission is None:
        return source, None
    home = os.environ.get(settings.emission.home_env, "").strip()
    if not home:
        raise ValueError(
            f"{settings.emission.home_env} is not set; the join's emission journal is relative to it"
        )
    return source, JsonlEmissionSink(Path(home) / settings.emission.journal_relpath)


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    wanted = {EnumHostProbe(probe) for probe in arguments.only or ()}
    declared = overlay.host(host)
    wants_join = EnumHostProbe.JOIN_J1 in (
        wanted or (set(declared.probes) if declared else set())
    )
    graph_source, sink = _join_j1_ports(overlay) if wants_join else (None, None)
    handler = HandlerHostReading(
        overlay=overlay,
        host=host,
        files=LocalHostFiles(),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        environ=os.environ,
        inventory=HostTableInventory(os.environ),
        canary_runner=SubprocessCanaryRoute(os.environ),
        adverts=_adverts(overlay),
        graph_source=graph_source,
        sink=sink,
    )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelHostReadingCommand(
            correlation_id=f"host-reading-{host}-{now}",
            probes=tuple(EnumHostProbe(probe) for probe in arguments.only or ()),
            write=write,
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"HOST-READING-FAILED {result.failure}\n")
        return 1
    for probe in result.probes:
        sys.stdout.write(
            f"READING host={probe.host} probe={probe.probe.value} verdict={probe.verdict.value} "
            f"outcome={probe.outcome.value} ledger={probe.ledger} key={probe.row_key} "
            f"reason={','.join(probe.reasons)}"
            + "".join(f" {name}={value}" for name, value in sorted(probe.cells.items()))
            + "\n"
        )
    failed = sum(probe.failed for probe in result.probes)
    sys.stdout.write(
        f"RUN host={host} probes={len(result.probes)} failed={failed} "
        f"write={'yes' if write else 'no'}\n"
    )
    return 1 if failed else 0


def _join_j2(arguments: Sequence[str]) -> int:
    """Run J2 for each pooled host (or the hosts named) on the bus chosen, and print one line each."""
    parser = argparse.ArgumentParser(prog="join-j2")
    parser.add_argument("--bus", choices=("kafka", "inmemory"), default="kafka")
    parser.add_argument("--bus-lane", default=None)
    parser.add_argument("--kafka-bootstrap", default=None)
    parser.add_argument("--workspace-root", default=os.environ.get("OMNI_HOME"))
    parser.add_argument("--host", action="append", default=None)
    args = parser.parse_args(list(arguments))
    overlay = load_overlay()
    if overlay.join_j2 is None:
        raise ValueError("the overlay has no join_j2 block")
    join = overlay.join_j2
    home = os.environ.get(join.emission_home_env, "").strip()
    if not home:
        raise ValueError(f"{join.emission_home_env} is not set")
    hosts = tuple(args.host or ()) or tuple(
        host.name for host in HostTableInventory(os.environ).hosts() if host.pooled
    )

    async def main() -> tuple[bool, list[str]]:
        async with open_lab_run_bus(
            bus=cast(BusKind, args.bus),
            lane=args.bus_lane,
            kafka_bootstrap=args.kafka_bootstrap,
            **{
                _WORKSPACE_KW: Path(args.workspace_root)
                if args.workspace_root
                else None
            },
        ) as bus:
            caller = LabWorkCaller(
                bus, wait_slack_seconds=join.settings.result_wait_slack_seconds
            )
            await caller.start()
            try:
                await caller.collect(join.collect_window_seconds)
                results = await HandlerJoinJ2(
                    settings=join.settings,
                    caller=caller,
                    sink=JsonlEmissionSink(Path(home) / join.emission_journal_relpath),
                    backend="real" if args.bus == "kafka" else "in_memory",
                ).run_hosts(hosts)
            finally:
                await caller.stop()
        lines = [
            f"J2 host={r.host} outcome={r.outcome.value} failure={r.failure_code or 'none'} "
            f"correlation={r.correlation_id} unit={r.work_unit_id or 'none'} "
            f"receipt_host={r.receipt_host or 'none'} advert_age_s={r.advert_age_seconds} "
            f"detail={r.detail!r}"
            for r in results
        ]
        golden = sum(r.ok for r in results)
        lines.append(
            f"RUN-J2 hosts={len(results)} golden={golden} failed={len(results) - golden}"
        )
        return golden == len(results), lines

    try:
        ok, lines = asyncio.run(main())
    except LabRunBusError as exc:
        sys.stderr.write(f"J2-BUS-FAILED {exc}\n")
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = HostReadingScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        result = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        if arguments and arguments[0] == "join-j2":
            return _join_j2(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("hourly", "probe"))
        parser.add_argument(
            "--only", action="append", choices=[probe.value for probe in EnumHostProbe]
        )
        args = parser.parse_args(arguments)
        return _run(args, write=args.verb == "hourly")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
