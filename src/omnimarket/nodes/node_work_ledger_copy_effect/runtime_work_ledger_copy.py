# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_copy_effect run``: one verified copy.

Verbs: ``run``; ``schedule-render``, ``schedule-install`` and ``schedule-uninstall`` for the hourly launchd agent.

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
from omnimarket.handlers.rules_unit_roots import UnitRootRefusedError
from omnimarket.nodes.node_work_ledger_copy_effect import schedule
from omnimarket.nodes.node_work_ledger_copy_effect.container.container_work_ledger_copy_effect import (
    create_handler_work_ledger_copy,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyOverlay,
    ModelCopyRequest,
)
from omnimarket.nodes.node_work_ledger_copy_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError


def _schedule(verb: str, overlay: ModelCopyOverlay) -> int:
    plan = schedule.load_schedule()
    agents = Path.home() / "Library" / "LaunchAgents"
    try:
        if verb == "schedule-render":
            sys.stdout.write(schedule.render_plist(overlay, plan).decode("utf-8"))
            return 0
        if verb == "schedule-install":
            return schedule.install(overlay, plan, agents_dir=agents, uid=os.getuid())
    except UnitRootRefusedError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return schedule.uninstall(plan, agents_dir=agents, uid=os.getuid())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "verb",
        choices=("run", "schedule-render", "schedule-install", "schedule-uninstall"),
    )
    args = parser.parse_args(argv)
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
