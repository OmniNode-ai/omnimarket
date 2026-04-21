# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pr_review_bot.

Runs the full PR review bot pipeline and outputs the verdict as JSON.

Usage:
    python -m omnimarket.nodes.node_pr_review_bot --pr-number 42 --repo owner/repo --models qwen3-coder-30b qwen3-14b
    python -m omnimarket.nodes.node_pr_review_bot --pr-number 42 --repo owner/repo --models qwen3-coder-30b --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from omnimarket.nodes.node_pr_review_bot.models.models import (
    EnumFindingSeverity,
    EnumPrVerdict,
)
from omnimarket.nodes.node_pr_review_bot.workflow_runner import run_review

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run the adversarial PR review bot on a pull request."
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
        help="GitHub repo (owner/repo)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="Reviewer model identifiers (at least one required)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default="deepseek-r1",
        help="Judge model identifier (default: deepseek-r1)",
    )
    parser.add_argument(
        "--severity-threshold",
        type=str,
        default="MAJOR",
        choices=[e.value for e in EnumFindingSeverity],
        help="Minimum severity to post a review thread (default: MAJOR)",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=20,
        help="Cap on review threads per PR (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Post no GitHub comments",
    )

    args = parser.parse_args()

    result = run_review(
        pr_number=args.pr_number,
        repo=args.repo,
        reviewer_models=args.models,
        judge_model=args.judge_model,
        severity_threshold=EnumFindingSeverity(args.severity_threshold),
        max_findings_per_pr=args.max_findings,
        dry_run=args.dry_run,
    )

    output = {
        "correlation_id": str(result.correlation_id),
        "verdict": result.verdict.model_dump(mode="json"),
        "event_count": len(result.events),
    }
    sys.stdout.write(json.dumps(output, indent=2) + "\n")

    if result.verdict.verdict != EnumPrVerdict.CLEAN:
        sys.exit(1)


if __name__ == "__main__":
    main()
