# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_post_mortem.

Usage:
    python -m omnimarket.nodes.node_session_post_mortem \
        --session-id <uuid> \
        --phases-planned build_loop,merge_sweep \
        --dry-run

Outputs JSON to stdout: ModelPostMortemHandlerResult model.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from omnimarket.nodes.node_session_post_mortem.handlers.handler_session_post_mortem import (
    HandlerSessionPostMortem,
    ModelPostMortemCommand,
)


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated string into a non-empty list."""
    return [v.strip() for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the session post-mortem collector."
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=str(uuid.uuid4()),
        help="Session UUID (default: generated)",
    )
    parser.add_argument(
        "--session-label",
        type=str,
        default="",
        help="Human-readable session label",
    )
    parser.add_argument(
        "--phases-planned",
        type=str,
        default="build_loop,merge_sweep,platform_readiness",
        help="Comma-separated planned phases",
    )
    parser.add_argument(
        "--phases-completed",
        type=str,
        default="",
        help="Comma-separated completed phases",
    )
    parser.add_argument(
        "--phases-failed",
        type=str,
        default="",
        help="Comma-separated failed phases",
    )
    parser.add_argument(
        "--phases-skipped",
        type=str,
        default="",
        help="Comma-separated skipped phases",
    )
    parser.add_argument(
        "--carry-forward",
        type=str,
        default="",
        help="Comma-separated ticket IDs to carry forward",
    )
    parser.add_argument(
        "--friction-dir",
        type=str,
        default=".onex_state/friction",
        help="Path to friction event directory",
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default="docs/post-mortems",
        help="Path to write post-mortem report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Skip filesystem writes",
    )

    args = parser.parse_args()

    command = ModelPostMortemCommand(
        session_id=args.session_id,
        session_label=args.session_label or f"{args.session_id[:8]} session",
        phases_planned=_split_csv(args.phases_planned),
        phases_completed=_split_csv(args.phases_completed),
        phases_failed=_split_csv(args.phases_failed),
        phases_skipped=_split_csv(args.phases_skipped),
        carry_forward_items=_split_csv(args.carry_forward),
        friction_dir=os.path.abspath(args.friction_dir),
        report_dir=os.path.abspath(args.report_dir),
        dry_run=args.dry_run,
    )

    handler = HandlerSessionPostMortem()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.outcome == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
