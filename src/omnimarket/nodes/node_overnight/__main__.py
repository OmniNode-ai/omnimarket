# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_overnight.

Usage:
    python -m omnimarket.nodes.node_overnight \
        --dry-run \
        --skip-build-loop

Outputs JSON to stdout: ModelOvernightResult model.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    HandlerOvernight,
    ModelOvernightCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the overnight autonomous pipeline."
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum build loop cycles (0 = unlimited)",
    )
    parser.add_argument("--skip-build-loop", action="store_true", default=False)
    parser.add_argument("--skip-merge-sweep", action="store_true", default=False)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run all phases in dry-run mode",
    )

    args = parser.parse_args()

    command = ModelOvernightCommand(
        correlation_id=str(uuid.uuid4()),
        max_cycles=args.max_cycles,
        skip_build_loop=args.skip_build_loop,
        skip_merge_sweep=args.skip_merge_sweep,
        dry_run=args.dry_run,
    )

    handler = HandlerOvernight()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.session_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
