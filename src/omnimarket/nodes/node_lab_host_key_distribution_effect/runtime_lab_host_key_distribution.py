# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line for distributing lab hosts' public SSH keys (OMN-22115)."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_lab_host_key_distribution import HandlerLabHostKeyDistribution
from .models.model_lab_host_key_distribution import ModelLabHostKeyDistributionRequest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="onex-host-key-distribution")
    parser.add_argument(
        "--host", action="append", required=True, help="host name (repeatable)"
    )
    parser.add_argument("--table", default=None, help="lab host table YAML path")
    parser.add_argument("--local-host", default=None, help="name of the current host")
    parser.add_argument(
        "--dry-run", action="store_true", help="plan appends without writing"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = HandlerLabHostKeyDistribution().handle(
        ModelLabHostKeyDistributionRequest(
            hosts=tuple(args.host),
            table_path=args.table,
            local_host=args.local_host,
            dry_run=args.dry_run,
        )
    )
    for line in result.lines():
        print(line, flush=True)
    if result.refusal:
        print(f"lab-host-key-distribution refused: {result.refusal}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
