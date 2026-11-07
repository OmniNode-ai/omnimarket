# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_reading_effect <verb>``.

``hourly``   read this host's probes and append the rows (what the schedule runs).
``probe``    the same reads, appending nothing.
``schedule`` check | install | uninstall | status | run-now, for a host in the overlay.

Exit codes: 0 every probe was read and written (a probe reading NOT WORKING is still 0: a probe
changes nothing and refuses nothing); 1 the run or a row failed (an unreadable source, an append
that did not land, a host the overlay does not declare); 3 a deployment fact is missing
(INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
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
