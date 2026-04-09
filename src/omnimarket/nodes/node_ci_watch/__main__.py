# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_ci_watch.

Usage:
    python -m omnimarket.nodes.node_ci_watch \
        --repo OmniNode-ai/omniclaude \
        --pr 42 \
        --dry-run

Outputs JSON to stdout: ModelCiWatchResult model.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    HandlerCiWatch,
    ModelCiWatchCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch CI status for a GitHub PR.")
    parser.add_argument("--repo", required=True, help="GitHub org/repo slug")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--timeout-minutes", type=int, default=60, help="Max wait time in minutes"
    )
    parser.add_argument(
        "--max-fix-cycles", type=int, default=3, help="Max auto-fix attempts"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Run without side effects"
    )

    args = parser.parse_args()

    command = ModelCiWatchCommand(
        pr_number=args.pr,
        repo=args.repo,
        correlation_id=str(uuid.uuid4()),
        timeout_minutes=args.timeout_minutes,
        max_fix_cycles=args.max_fix_cycles,
        dry_run=args.dry_run,
    )

    handler = HandlerCiWatch()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.terminal_status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
