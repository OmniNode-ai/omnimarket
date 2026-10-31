# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex-claude-pool-refill``: the Claude pool refill command line (OMN-21626).

Fills each lab host's Claude Code token pool from the one pool in the secret store, probes the
credential the host's lane runner draws, and moves the host past an expired, revoked or refused token.
Every host, key, path and answer pattern is the pool overlay's; this file holds the flags only.
"""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_claude_pool_refill import HandlerClaudePoolRefill
from .models.model_claude_pool_refill import ModelClaudePoolRefillRequest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="onex-claude-pool-refill")
    parser.add_argument(
        "--config",
        default=None,
        help="the pool overlay (default: under ONEX_WORKSPACE_CONFIG_ROOT)",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="a host name from the overlay (repeatable; default: every declared host)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="load and validate the overlay, then stop",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="read and probe only: no add, no mark"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(
            config_path=args.config,
            hosts=tuple(args.host),
            dry_run=args.dry_run,
            check_config=args.check_config,
        )
    )
    for line in result.lines:
        print(line, flush=True)
    if result.refusal:
        print(f"claude-pool-refill refused: {result.refusal}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
