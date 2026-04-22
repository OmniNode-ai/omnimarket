# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_fixer_dispatcher.

Routes a PR stall event to the correct fixer node and outputs the dispatch spec as JSON.

Usage:
    python -m omnimarket.nodes.node_fixer_dispatcher --pr-number 42 --repo omnimarket --stall-category red
    python -m omnimarket.nodes.node_fixer_dispatcher --pr-number 42 --repo omnimarket --stall-category conflicted --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_fixer_dispatcher.handlers.handler_fixer_dispatcher import (
    HandlerFixerDispatcher,
)
from omnimarket.nodes.node_fixer_dispatcher.models.model_fixer_dispatch import (
    EnumFixerAction,
    EnumStallCategory,
    ModelFixerDispatchRequest,
)

_log = logging.getLogger(__name__)

_STALL_CATEGORIES = [
    EnumStallCategory.RED,
    EnumStallCategory.CONFLICTED,
    EnumStallCategory.BEHIND,
    EnumStallCategory.DEPLOY_GATE,
    EnumStallCategory.UNKNOWN,
    EnumStallCategory.STALE,
]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Route a PR stall event to the correct fixer node."
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="GitHub PR number",
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="GitHub repo slug (e.g. 'omnimarket')",
    )
    parser.add_argument(
        "--stall-category",
        type=str,
        required=True,
        choices=_STALL_CATEGORIES,
        help="Stall category: red, conflicted, behind, deploy_gate, unknown, stale",
    )
    parser.add_argument(
        "--blocking-reason",
        type=str,
        default="",
        help="Human-readable blocking reason from stall detector",
    )
    parser.add_argument(
        "--stall-count",
        type=int,
        default=1,
        help="Number of consecutive identical snapshots (default: 1)",
    )
    parser.add_argument(
        "--head-sha",
        type=str,
        default=None,
        help="HEAD SHA at time of stall detection",
    )
    parser.add_argument(
        "--branch-name",
        type=str,
        default="",
        help="Branch name for the PR",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Return dispatch spec without executing",
    )

    args = parser.parse_args()

    request = ModelFixerDispatchRequest(
        pr_number=args.pr_number,
        repo=args.repo,
        stall_category=args.stall_category,
        blocking_reason=args.blocking_reason,
        stall_count=args.stall_count,
        head_sha=args.head_sha,
        branch_name=args.branch_name,
        dry_run=args.dry_run,
    )

    handler = HandlerFixerDispatcher()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.action == EnumFixerAction.ESCALATE:
        sys.exit(1)


if __name__ == "__main__":
    main()
