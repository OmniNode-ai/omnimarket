# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pr_polish.

Initializes the PR polish FSM and outputs the initial state as JSON.
Phase execution is orchestrated by the caller using emitted phase events.

Usage:
    python -m omnimarket.nodes.node_pr_polish --pr-number 42
    python -m omnimarket.nodes.node_pr_polish --skip-conflicts --dry-run

Outputs JSON to stdout: ModelPrPolishState model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_pr_polish.handlers.handler_pr_polish import (
    HandlerPrPolish,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Initialize the PR polish FSM for a pull request."
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=None,
        help="PR number to polish (auto-detected from current branch if omitted)",
    )
    parser.add_argument(
        "--skip-conflicts",
        action="store_true",
        default=False,
        help="Skip merge conflict resolution phase",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log phase decisions without side effects",
    )

    args = parser.parse_args()

    command = ModelPrPolishStartCommand(
        correlation_id=uuid4(),
        pr_number=args.pr_number,
        skip_conflicts=args.skip_conflicts,
        dry_run=args.dry_run,
        requested_at=datetime.now(UTC),
    )

    handler = HandlerPrPolish()
    state = handler.start(command)

    sys.stdout.write(state.model_dump_json(indent=2) + "\n")

    if state.current_phase.value in ("FAILED",):
        sys.exit(1)


if __name__ == "__main__":
    main()
