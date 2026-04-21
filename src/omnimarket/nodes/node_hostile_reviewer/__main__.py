# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_hostile_reviewer.

Initializes the hostile reviewer FSM and runs a full adversarial review pipeline.
Outputs the completed event as JSON.

Usage:
    python -m omnimarket.nodes.node_hostile_reviewer --pr-number 42 --repo owner/repo
    python -m omnimarket.nodes.node_hostile_reviewer --file-path src/foo.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_hostile_reviewer import (
    HandlerHostileReviewer,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run multi-model adversarial code review."
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=None,
        help="PR number to review",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="GitHub repo (owner/repo)",
    )
    parser.add_argument(
        "--file-path",
        type=str,
        default=None,
        help="File path to review (alternative to PR)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=["codex", "deepseek-r1"],
        help="Models to use for review",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=10,
        help="Max review passes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run without side effects",
    )

    args = parser.parse_args()

    if not args.pr_number and not args.file_path:
        parser.error("Either --pr-number or --file-path is required")

    if args.pr_number is not None and args.repo is None:
        parser.error("--pr-number requires --repo")

    command = ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        pr_number=args.pr_number,
        repo=args.repo,
        file_path=args.file_path,
        models=args.models,
        max_passes=args.max_passes,
        dry_run=args.dry_run,
        requested_at=datetime.now(tz=UTC),
    )

    handler = HandlerHostileReviewer()
    completed = handler.handle(command)

    sys.stdout.write(completed.model_dump_json(indent=2) + "\n")

    if completed.final_phase != EnumHostileReviewerPhase.DONE:
        sys.exit(1)


if __name__ == "__main__":
    main()
