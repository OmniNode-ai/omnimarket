# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_cross_cli_originator.

Invoked by opencode/codex plugins to publish a delegation envelope.

Usage:
    python -m omnimarket.nodes.node_cross_cli_originator --prompt "Run OMN-1234"
    python -m omnimarket.nodes.node_cross_cli_originator --prompt "..." --task-type research --session-id abc123
    python -m omnimarket.nodes.node_cross_cli_originator --json-input '{"prompt": "...", "task_type": "research"}'

Outputs JSON to stdout: ModelCrossCliOriginatorResult model.
Exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import logging
import sys

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Publish a cross-CLI delegation envelope to the event bus.",
        prog="python -m omnimarket.nodes.node_cross_cli_originator",
    )
    parser.add_argument("--prompt", default=None, help="Delegation prompt text.")
    parser.add_argument(
        "--task-type",
        default="research",
        help="Task type hint (default: research).",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional caller session ID for correlation.",
    )
    parser.add_argument(
        "--correlation-id",
        default=None,
        help="Optional pre-assigned UUID correlation ID.",
    )
    parser.add_argument(
        "--json-input",
        default=None,
        help="Raw JSON ModelCrossCliOriginatorInput (overrides individual flags).",
    )

    args = parser.parse_args()

    from omnimarket.nodes.node_cross_cli_originator.handlers.handler_cross_cli_originator import (
        HandlerCrossCliOriginator,
    )
    from omnimarket.nodes.node_cross_cli_originator.models.model_cross_cli_originator_input import (
        ModelCrossCliOriginatorInput,
    )

    if args.json_input:
        command = ModelCrossCliOriginatorInput.model_validate_json(args.json_input)
    else:
        if not args.prompt:
            parser.error("--prompt is required unless --json-input is provided.")

        from uuid import UUID

        correlation_id = UUID(args.correlation_id) if args.correlation_id else None
        command = ModelCrossCliOriginatorInput(
            prompt=args.prompt,
            task_type=args.task_type,
            session_id=args.session_id,
            correlation_id=correlation_id,
        )

    handler = HandlerCrossCliOriginator()
    try:
        result = handler.handle(command)
    except Exception as exc:
        _log.error("cross_cli_originator failed: %s", exc)
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(1)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
