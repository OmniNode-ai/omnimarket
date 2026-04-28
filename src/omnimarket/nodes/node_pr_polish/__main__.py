# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pr_polish.

Runs the real repo/worktree-aware PR polish path and emits the terminal
``ModelPrPolishCompletedEvent`` as JSON. The pure FSM remains in
``handler_pr_polish.py`` for unit coverage; this CLI owns the live
branch-polishing execution surface used by merge-sweep dispatch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.workflow_runner import run_live_pr_polish

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run live PR polish for a pull request worktree."
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="GitHub repo slug (owner/repo).",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="PR number to polish.",
    )
    parser.add_argument(
        "--ticket",
        type=str,
        default=None,
        help="Optional Linear ticket identifier for breadcrumb tracing.",
    )
    parser.add_argument(
        "--required-clean-runs",
        type=int,
        default=4,
        help="Consecutive clean local-review passes required before done.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum local-review cycles before stopping.",
    )
    parser.add_argument(
        "--skip-conflicts",
        action="store_true",
        default=False,
        help="Skip merge conflict resolution phase",
    )
    parser.add_argument(
        "--skip-pr-review",
        action="store_true",
        default=False,
        help="Skip PR review comments / CI repair phase",
    )
    parser.add_argument(
        "--skip-local-review",
        action="store_true",
        default=False,
        help="Skip local review phase",
    )
    parser.add_argument(
        "--no-ci",
        action="store_true",
        default=False,
        help="Skip CI fetch in the PR review phase",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        default=False,
        help="Apply fixes locally without pushing",
    )
    parser.add_argument(
        "--no-automerge",
        action="store_true",
        default=False,
        help="Skip enabling GitHub automerge at the end",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log phase decisions without side effects",
    )
    parser.add_argument(
        "--worktree-path",
        type=str,
        default=None,
        help="Explicit worktree path override.",
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Explicit run directory for breadcrumb/result.json persistence.",
    )

    args = parser.parse_args()

    command = ModelPrPolishStartCommand(
        correlation_id=uuid4(),
        repo=args.repo,
        pr_number=args.pr_number,
        ticket_id=args.ticket,
        required_clean_runs=args.required_clean_runs,
        max_iterations=args.max_iterations,
        skip_conflicts=args.skip_conflicts,
        skip_pr_review=args.skip_pr_review,
        skip_local_review=args.skip_local_review,
        no_ci=args.no_ci,
        no_push=args.no_push,
        no_automerge=args.no_automerge,
        dry_run=args.dry_run,
        worktree_path=args.worktree_path,
        run_dir=args.run_dir,
        requested_at=datetime.now(UTC),
    )

    completed = run_live_pr_polish(command)
    sys.stdout.write(completed.model_dump_json(indent=2) + "\n")
    if completed.final_phase.value == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
