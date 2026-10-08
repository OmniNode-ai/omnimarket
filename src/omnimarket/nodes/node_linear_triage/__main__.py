# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_linear_triage.

Assesses the declared sprint project against GitHub PR state, reports close
candidates, and gates live Done writes on an acceptance-bound PASS dod_verify.

Requires:
  LINEAR_API_KEY and GITHUB_TOKEN — keys in the declared secret store
  project_id or LINEAR_ACTIVE_SPRINT_PROJECT_ID — current sprint project UUID

Usage:
    python -m omnimarket.nodes.node_linear_triage --project-id <sprint-uuid>
    python -m omnimarket.nodes.node_linear_triage --project-id <sprint-uuid> --local-secrets --dry-run
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
from pathlib import Path

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
    """Run handler.handle with a wall-clock timeout.

    ``handle`` is now ``async def`` (OMN-13710: resolved via ``resolve_api_key_async``),
    so we can await it directly inside ``asyncio.wait_for``.  The old
    ``run_in_executor`` indirection is no longer needed.
    """
    return await asyncio.wait_for(
        handler.handle(command),
        timeout=float(timeout),
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Scan Linear tickets and reconcile against GitHub PR state."
    )
    parser.add_argument("--scope", choices=("sprint", "backlog"), default="sprint")
    parser.add_argument("--project-id", default="", help="Current sprint project UUID.")
    secrets = parser.add_mutually_exclusive_group()
    secrets.add_argument("--secret-resolver-config-path", default="")
    secrets.add_argument(
        "--local-secrets",
        action="store_true",
        help="Select the packaged explicit env mapping for exported Linear/GitHub keys.",
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
        scope=args.scope,
        project_id=args.project_id,
        secret_resolver_config_path=(
            str(Path(__file__).with_name("local_secret_resolver.yaml"))
            if args.local_secrets
            else args.secret_resolver_config_path
        ),
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
