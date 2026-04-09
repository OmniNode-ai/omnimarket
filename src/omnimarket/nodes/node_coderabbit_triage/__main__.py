# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_coderabbit_triage.

Usage:
    python -m omnimarket.nodes.node_coderabbit_triage \
        --repo OmniNode-ai/omniclaude \
        --pr 42 \
        --dry-run

Outputs JSON to stdout: ModelCoderabbitTriageResult model.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage CodeRabbit review threads on a GitHub PR."
    )
    parser.add_argument("--repo", required=True, help="GitHub org/repo slug")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Classify but do not post replies or resolve threads",
    )

    args = parser.parse_args()

    command = ModelCoderabbitTriageCommand(
        repo=args.repo,
        pr_number=args.pr,
        correlation_id=str(uuid.uuid4()),
        dry_run=args.dry_run,
    )

    handler = HandlerCoderabbitTriage()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
