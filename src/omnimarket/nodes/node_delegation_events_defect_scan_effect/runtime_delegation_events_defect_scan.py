# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_events_defect_scan_effect <verb>``.

``scan``      read the day, run the defect-scan compute node and append the row (what the daily timer runs).
``probe``     the same read, appending nothing.
``schedule``  ``install`` writes the daily systemd user timer from the contract's ``schedule`` block; ``uninstall`` removes it.

Exit codes: 0 the scan ran and its row was written (NOT WORKING and UNKNOWN are still 0: a reading changes nothing and refuses
nothing); 1 the run failed (a row that did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanOverlay
from omnimarket.nodes.node_delegation_events_defect_scan_effect.adapters.events_source import (
    SshPostgresDefectEventSource,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers import (
    schedule as schedule_module,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers.handler_delegation_events_defect_scan_run import (
    HandlerDelegationEventsDefectScanRun,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.overlay import (
    NODE_NAME,
    ModelDefectScanEffectOverlay,
    load_overlay,
    load_scan_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def build_handler(
    overlay: ModelDefectScanEffectOverlay,
    scan_overlay: ModelDefectScanOverlay,
    environ: Mapping[str, str],
    write: bool,
) -> HandlerDelegationEventsDefectScanRun:
    return HandlerDelegationEventsDefectScanRun(
        scan_overlay=scan_overlay,
        source=SshPostgresDefectEventSource(overlay.events_source, environ),
        appender=build_appender(overlay.ledger_append, scan_overlay.row_lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )


def _run(write: bool) -> int:
    overlay = load_overlay()
    scan_overlay = load_scan_overlay()
    environ = os.environ  # ONEX_FLAG_EXEMPT: variable names are declared by the overlay
    handler = build_handler(overlay, scan_overlay, environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelDefectScanCommand(correlation_id=f"defect-scan-{now}", write=write)
    )
    if result.status == "failed":
        sys.stderr.write(f"DEFECT-SCAN-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING verdict={result.verdict.value} missing_terminal={result.missing_terminal} "
        f"cost_unread_as_zero={result.cost_unread_as_zero} unattributed={result.unattributed} "
        f"rows={result.rows_scanned} ledger={result.ledger} "
        f"reason={','.join(result.reasons) or 'none'}\n"
    )
    return 0


def _schedule(action: str) -> int:
    schedule = schedule_module.load_schedule()
    home = Path.home()
    if action == "uninstall":
        return schedule_module.uninstall(schedule, home=home)
    overlay = load_overlay()
    return schedule_module.install(
        schedule,
        omni_root=Path(
            os.environ["OMNI_HOME"]
        ),  # ONEX_FLAG_EXEMPT: the registry root the unit runs from
        home=home,
        python=Path(sys.executable),
        extra_environment_names=overlay.carried_environment_names(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("scan", "probe", "schedule"))
        parser.add_argument("action", nargs="?", choices=("install", "uninstall"))
        args = parser.parse_args(arguments)
        if args.verb == "schedule":
            if args.action is None:
                parser.error("schedule needs install or uninstall")
            return _schedule(args.action)
        return _run(write=args.verb == "scan")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
        KeyError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
