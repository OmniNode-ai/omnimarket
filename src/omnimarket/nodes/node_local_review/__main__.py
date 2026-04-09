# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_local_review.

Initializes the local review loop FSM and outputs the initial state as JSON.
Phase execution (review → fix → commit cycles) is orchestrated by the caller.

Usage:
    python -m omnimarket.nodes.node_local_review
    python -m omnimarket.nodes.node_local_review --max-iterations 5 --required-clean-runs 2
    python -m omnimarket.nodes.node_local_review --dry-run

Outputs JSON to stdout: ModelLocalReviewState model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Initialize the local review loop FSM."
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum review-fix cycles before stopping (default: 10)",
    )
    parser.add_argument(
        "--required-clean-runs",
        type=int,
        default=2,
        help="Consecutive clean review passes required before done (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log review decisions without making changes",
    )

    args = parser.parse_args()

    command = ModelLocalReviewStartCommand(
        correlation_id=uuid4(),
        max_iterations=args.max_iterations,
        required_clean_runs=args.required_clean_runs,
        dry_run=args.dry_run,
        requested_at=datetime.now(UTC),
    )

    handler = HandlerLocalReview()
    state = handler.start(command)

    sys.stdout.write(state.model_dump_json(indent=2) + "\n")

    if state.current_phase.value in ("FAILED",):
        sys.exit(1)


if __name__ == "__main__":
    main()
