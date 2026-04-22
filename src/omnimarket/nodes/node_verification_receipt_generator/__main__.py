# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_verification_receipt_generator.

Generates an evidence receipt for a task-completed claim and outputs it as JSON.

Usage:
    python -m omnimarket.nodes.node_verification_receipt_generator --task-id OMN-9403 --claim "all tests pass" --repo omnimarket --pr-number 370
    python -m omnimarket.nodes.node_verification_receipt_generator --task-id OMN-9403 --claim "all tests pass" --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)
from omnimarket.nodes.node_verification_receipt_generator.models.model_verification_receipt import (
    ModelVerificationReceiptRequest,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Generate an evidence receipt for a task-completed claim."
    )
    parser.add_argument(
        "--task-id",
        type=str,
        required=True,
        help="Task identifier (e.g. 'OMN-9403')",
    )
    parser.add_argument(
        "--claim",
        type=str,
        required=True,
        help="What the task claims to have done (e.g. 'all tests pass')",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default="",
        help="GitHub repo slug for CI verification",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=None,
        help="PR number to verify CI checks for",
    )
    parser.add_argument(
        "--worktree-path",
        type=str,
        default="",
        help="Path to the worktree for pytest verification",
    )
    parser.add_argument(
        "--no-verify-ci",
        action="store_true",
        default=False,
        help="Skip CI check verification",
    )
    parser.add_argument(
        "--no-verify-tests",
        action="store_true",
        default=False,
        help="Skip pytest verification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Return receipt without running verification",
    )

    args = parser.parse_args()

    if args.no_verify_ci and args.no_verify_tests and not args.dry_run:
        parser.error("At least one verification must be enabled, or pass --dry-run.")

    request = ModelVerificationReceiptRequest(
        task_id=args.task_id,
        claim=args.claim,
        repo=args.repo,
        pr_number=args.pr_number,
        worktree_path=args.worktree_path,
        verify_ci=not args.no_verify_ci,
        verify_tests=not args.no_verify_tests,
        dry_run=args.dry_run,
    )

    handler = HandlerVerificationReceiptGenerator()
    receipt = handler.handle(request)

    sys.stdout.write(receipt.model_dump_json(indent=2) + "\n")

    if not receipt.overall_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
