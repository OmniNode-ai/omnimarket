# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_linear_triage.

Scans all non-completed Linear tickets, verifies status against actual GitHub
PR state, auto-marks merged tickets done, and flags stale tickets for review.

Requires:
  LINEAR_API_KEY — Linear personal API key

Usage:
    python -m omnimarket.nodes.node_linear_triage
    python -m omnimarket.nodes.node_linear_triage --dry-run
    python -m omnimarket.nodes.node_linear_triage --threshold-days 7
    python -m omnimarket.nodes.node_linear_triage --team "Omninode" --dry-run
    python -m omnimarket.nodes.node_linear_triage --timeout 120

Outputs JSON to stdout: ModelLinearTriageResult model.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
)

_log = logging.getLogger(__name__)


async def _run_with_timeout(
    handler: HandlerLinearTriage,
    command: ModelLinearTriageStartCommand,
    timeout: int,
) -> ModelLinearTriageResult:
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return await asyncio.wait_for(
            loop.run_in_executor(pool, handler.handle, command),
            timeout=float(timeout),
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Scan Linear tickets and reconcile against GitHub PR state."
    )
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=14,
        dest="threshold_days",
        help="Tickets updated within this many days are checked against PR state (default: 14).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Assess and report without writing any changes to Linear.",
    )
    parser.add_argument(
        "--team",
        default="Omninode",
        help="Linear team name (default: Omninode).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        dest="timeout",
        help="Maximum seconds to run before aborting (default: 300).",
    )

    args = parser.parse_args()

    command = ModelLinearTriageStartCommand(
        threshold_days=args.threshold_days,
        dry_run=args.dry_run,
        team=args.team,
    )

    handler = HandlerLinearTriage()

    try:
        result = asyncio.run(_run_with_timeout(handler, command, args.timeout))
    except TimeoutError:
        sys.stderr.write(
            f"\nERROR: node_linear_triage timed out after {args.timeout}s. "
            "Increase --timeout or investigate slow API calls.\n"
        )
        sys.exit(2)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    # Print human-readable summary to stderr
    total = result.total_scanned
    n = args.threshold_days
    sys.stderr.write(
        f"\n{'=' * 44}\n"
        f"Linear Triage Report\n"
        f"{'=' * 44}\n"
        f"Scanned:         {total} tickets\n"
        f"Recent (<{n}d):  {result.recent_count} tickets\n"
        f"Stale (>{n}d):   {result.stale_count} tickets\n"
        f"\n"
        f"Marked done:          {result.marked_done} "
        f"(incl. {result.marked_done_superseded} superseded, {result.epics_closed} epics)\n"
        f"Stale flags:          {result.stale_flagged} (human review needed)\n"
        f"Orphans:              {result.orphaned} (no parent epic)\n"
        f"{'=' * 44}\n"
    )

    if result.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
