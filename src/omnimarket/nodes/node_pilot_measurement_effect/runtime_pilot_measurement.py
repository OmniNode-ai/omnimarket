# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_pilot_measurement_effect <verb>``.

``daily``    measure every declared pilot against its frozen bar and append the row.
``probe``    the same read, appending nothing.
``schedule`` install | uninstall | status | run-now, for a host in the overlay's schedule_hosts.

Exit codes: 0 the read ran and its row was written (a reading of NOT WORKING is still 0: a reading
changes nothing and refuses nothing); 1 the run or its row failed (no appender for a writing run,
an append that did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_pilot_measurement_effect.adapters.file_result_source import (
    FileResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.schedule import (
    PilotMeasurementScheduler,
    load_schedule,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    EnumPilotState,
    ModelPilotCounts,
    ModelPilotMeasurementCommand,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    NODE_NAME,
    ModelPilotMeasurementOverlay,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelPilotMeasurementOverlay, environ: Mapping[str, str], write: bool
) -> tuple[HandlerPilotMeasurement, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    handler = HandlerPilotMeasurement(
        overlay=overlay,
        host=host,
        results=FileResultSource(environ),
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
        emissions=JsonlEmissionSink(
            Path(_required(overlay.emission.journal_env, environ))
        ),
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelPilotMeasurementCommand(
            correlation_id=f"pilot-measurement-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"PILOT-MEASUREMENT-FAILED {result.failure}\n")
        return 1
    counts = result.counts or ModelPilotCounts()
    sys.stdout.write(
        f"READING host={result.host} "
        f"verdict={result.verdict.value if result.verdict else 'none'} "
        f"outcome={result.outcome} pilots={counts.pilots} "
        f"accepted={counts.accepted} rejected={counts.rejected} "
        f"refused={counts.refused} unmeasured={counts.unmeasured} "
        f"ledger={result.ledger} key={result.row_key} "
        f"reason={','.join(result.reasons)}\n"
    )
    for outcome in result.outcomes:
        if outcome.state is EnumPilotState.ACCEPTED:
            continue
        sys.stdout.write(
            f"PILOT pilot={outcome.pilot} state={outcome.state.value} "
            f"reasons={','.join(outcome.reasons) or '-'}\n"
        )
    return 1 if result.ledger.startswith("append-failed") else 0


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
    scheduler = PilotMeasurementScheduler(
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
        parser.add_argument("verb", choices=("daily", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "daily")
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
