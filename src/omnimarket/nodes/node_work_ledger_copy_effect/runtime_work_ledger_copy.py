# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_copy_effect run``: one verified copy.

Verbs: ``run``; ``snapshot`` and ``drill`` (OMN-22916: exit 0 written, or restored and equal; 1 refused or unequal); ``schedule-render``, ``schedule-install`` and ``schedule-uninstall`` for the hourly launchd agent.

Exit codes: 0 copied and verified on the scratch database; 1 any other typed outcome; 3 a deployment
fact is missing (no overlay); 4 the run's STATUS row could not be appended.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_work_ledger_copy_effect import schedule
from omnimarket.nodes.node_work_ledger_copy_effect.container.container_work_ledger_copy_effect import (
    create_handler_work_ledger_copy,
    create_handler_work_ledger_restore_drill,
    create_handler_work_ledger_snapshot,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyOverlay,
    ModelCopyRequest,
    ModelDrillRequest,
    ModelSnapshotRequest,
)
from omnimarket.nodes.node_work_ledger_copy_effect.overlay import (
    load_overlay,
    load_snapshot_overlay,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError


def _schedule(verb: str, overlay: ModelCopyOverlay) -> int:
    plan = schedule.load_schedule()
    agents = Path.home() / "Library" / "LaunchAgents"
    if verb == "schedule-render":
        sys.stdout.write(schedule.render_plist(overlay, plan).decode("utf-8"))
        return 0
    if verb == "schedule-install":
        return schedule.install(overlay, plan, agents_dir=agents, uid=os.getuid())
    return schedule.uninstall(plan, agents_dir=agents, uid=os.getuid())


def _snapshot_verb(verb: str) -> int:
    """``snapshot``: write one snapshot to lab storage. ``drill``: restore the newest one and compare."""
    try:
        overlay = load_snapshot_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE {exc}" + "\n")
        return 3
    started = datetime.now(UTC)
    try:
        if verb == "snapshot":
            made = create_handler_work_ledger_snapshot(overlay, os.environ).handle(
                ModelSnapshotRequest(
                    run_id=started.strftime("%Y%m%dT%H%M%SZ"), started_at=started
                )
            )
            sys.stdout.write(f"{made.outcome.value} run={made.run_id} {made.detail}\n")
            return 0 if made.ok else 1
        drilled = create_handler_work_ledger_restore_drill(overlay, os.environ).handle(
            ModelDrillRequest(started_at=started)
        )
    except CopyStepError as exc:
        sys.stderr.write(f"REPORT-FAILED {exc}" + "\n")
        return 4
    sys.stdout.write(drilled.receipt() + "\n")
    return 0 if drilled.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "verb",
        choices=(
            "run",
            "snapshot",
            "drill",
            "schedule-render",
            "schedule-install",
            "schedule-uninstall",
        ),
    )
    args = parser.parse_args(argv)
    if args.verb in ("snapshot", "drill"):
        return _snapshot_verb(args.verb)
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE {exc}" + "\n")
        return 3
    if args.verb.startswith("schedule-"):
        return _schedule(args.verb, overlay)
    started = datetime.now(UTC)
    request = ModelCopyRequest(
        run_id=started.strftime("%Y%m%dT%H%M%SZ"), started_at=started
    )
    try:
        result = create_handler_work_ledger_copy(overlay, os.environ).handle(request)
    except CopyStepError as exc:
        sys.stderr.write(f"REPORT-FAILED {exc}" + "\n")
        return 4
    sys.stdout.write(f"{result.outcome.value} run={result.run_id} {result.detail}\n")
    return 0 if result.ok else 1
