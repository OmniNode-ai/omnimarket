# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_start_path_drill_effect <verb>``.

``read``   run the drill, append the observe-log rows and the reading row (what a schedule runs); ``daily`` is the same.
``probe``  the same drill, appending nothing.
``schedule`` install | uninstall | status | run-now, for a host in the overlay's schedule_hosts.

Exit codes: 0 the drill ran and its rows were written (NOT_WORKING is still 0: a reading changes nothing and
refuses nothing); 1 the drill or its rows failed (an append that did not land); 3 a deployment fact is missing
(INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_in_memory import (
    InMemoryStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_ledger import (
    LedgerStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.overlay import (
    load_start_admission_policy,
)
from omnimarket.nodes.node_start_path_drill_effect.handlers.handler_start_path_drill import (
    HandlerStartPathDrill,
    load_declared_paths,
)
from omnimarket.nodes.node_start_path_drill_effect.handlers.schedule import (
    StartPathDrillScheduler,
    load_schedule,
)
from omnimarket.nodes.node_start_path_drill_effect.models.model_start_path_drill import (
    ModelStartPathDrillCommand,
)
from omnimarket.nodes.node_start_path_drill_effect.overlay import (
    NODE_NAME,
    ModelStartPathDrillOverlay,
    load_overlay,
)
from omnimarket.protocols.protocol_start_admission import ProtocolStartAdmissionLog


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelStartPathDrillOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerStartPathDrill, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    admission_paths = tuple(load_start_admission_policy(environ).modes)
    log: ProtocolStartAdmissionLog = InMemoryStartAdmissionLog()
    appender = None
    if write:
        appender = build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        log = LedgerStartAdmissionLog(
            appender,
            lane=overlay.publish.observe_lane,
            actor=overlay.publish.actor,
            host=host,
        )
    handler = HandlerStartPathDrill(
        overlay=overlay,
        host=host,
        declared_paths=load_declared_paths(),
        admission_paths=admission_paths,
        log=log,
        appender=appender,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelStartPathDrillCommand(
            correlation_id=f"start-path-drill-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"START-PATH-DRILL-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} census={result.census_paths} "
        f"bound={result.bound} unbound={result.unbound} dead={result.dead} "
        f"refused={result.refused} admitted={result.admitted} "
        f"control_admitted={result.control_admitted} "
        f"log_written={result.log_written} log_failed={result.log_failed} "
        f"ledger={result.ledger} key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    return 1 if result.failed else 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
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
    scheduler = StartPathDrillScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "install":
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
        parser.add_argument("verb", choices=("daily", "read", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb in ("daily", "read"))
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
