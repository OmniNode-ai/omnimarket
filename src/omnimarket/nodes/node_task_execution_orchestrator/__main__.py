# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_task_execution_orchestrator.

Plans the generic task.execute route for a raw prompt and prints the resulting
ModelTaskExecutionResult as JSON. Dry-run only — performs no side effects.

Usage:
    python -m omnimarket.nodes.node_task_execution_orchestrator \\
        --prompt "refactor the config loader" --target-repo omnibase_core
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    HandlerTaskExecutionOrchestrator,
    UnsupportedTaskActionError,
)
from omnimarket.nodes.node_task_execution_orchestrator.models.model_task_execution import (
    ModelTaskExecutionRequest,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Plan the generic task.execute route for a prompt (dry-run)."
    )
    parser.add_argument(
        "--prompt",
        type=str,
        required=True,
        help="Raw coding/mechanical task prompt to normalize and route-plan",
    )
    parser.add_argument(
        "--target-repo",
        type=str,
        default=None,
        help="Optional target repo applied when normalizing the prompt",
    )
    parser.add_argument(
        "--ticket-id",
        type=str,
        default=None,
        help="Optional parent ticket applied when normalizing the prompt",
    )

    args = parser.parse_args()

    request = ModelTaskExecutionRequest(
        prompt=args.prompt,
        target_repo=args.target_repo,
        ticket_id=args.ticket_id,
        dry_run=True,
    )

    handler = HandlerTaskExecutionOrchestrator()
    try:
        result = handler.handle(request)
    except UnsupportedTaskActionError as exc:
        sys.stderr.write(f"unsupported: {exc.reason}\n")
        sys.exit(1)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
