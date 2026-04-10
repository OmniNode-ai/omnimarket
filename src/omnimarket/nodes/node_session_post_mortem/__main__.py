# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_post_mortem.

Usage:
    python -m omnimarket.nodes.node_session_post_mortem \\
        --session-id <uuid> \\
        --session-label "2026-04-10 overnight" \\
        --phases-planned build_loop,merge_sweep \\
        --phases-completed merge_sweep \\
        --dry-run

Outputs JSON to stdout: ModelPostMortemResult model.
Exits 1 if outcome is FAILED.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date

from omnimarket.nodes.node_session_post_mortem.handlers.handler_session_post_mortem import (
    EnumPostMortemOutcome,
    HandlerSessionPostMortem,
    ModelPostMortemCommand,
)


def _split_csv(value: str) -> list[str]:
    """Split comma-separated string, filtering empty tokens."""
    return [v.strip() for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the session post-mortem node.")
    parser.add_argument(
        "--session-id",
        default=str(uuid.uuid4()),
        help="Unique session ID",
    )
    parser.add_argument(
        "--session-label",
        default=f"{date.today().isoformat()} overnight",
        help="Human-readable session label",
    )
    parser.add_argument(
        "--phases-planned",
        default="build_loop,merge_sweep,platform_readiness",
        help="Comma-separated planned phases",
    )
    parser.add_argument(
        "--phases-completed",
        default="",
        help="Comma-separated completed phases",
    )
    parser.add_argument(
        "--phases-failed",
        default="",
        help="Comma-separated failed phases",
    )
    parser.add_argument(
        "--phases-skipped",
        default="",
        help="Comma-separated skipped phases",
    )
    parser.add_argument(
        "--carry-forward",
        default="",
        help="Comma-separated carry-forward ticket IDs",
    )
    parser.add_argument(
        "--friction-dir",
        default=".onex_state/friction",
        help="Path to friction events directory",
    )
    parser.add_argument(
        "--report-dir",
        default="docs/post-mortems",
        help="Directory to write the Markdown report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate without writing to filesystem",
    )

    args = parser.parse_args()

    command = ModelPostMortemCommand(
        session_id=args.session_id,
        session_label=args.session_label,
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

    if result.outcome == EnumPostMortemOutcome.FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
