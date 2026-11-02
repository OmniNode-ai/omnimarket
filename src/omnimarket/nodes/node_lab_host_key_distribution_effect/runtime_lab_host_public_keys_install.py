# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line installing the overlay-declared lab host public keys."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_lab_host_public_keys_install import (
    HandlerLabHostPublicKeysInstall,
)
from .models.model_lab_host_key_distribution import ModelDeclaredKeysInstallRequest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="onex-declared-public-keys-install")
    parser.add_argument(
        "--host", action="append", default=None, help="host name (repeatable)"
    )
    parser.add_argument("--table", default=None, help="lab host table YAML path")
    parser.add_argument("--local-host", default=None, help="name of the current host")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = HandlerLabHostPublicKeysInstall().handle(
        ModelDeclaredKeysInstallRequest(
            hosts=tuple(args.host) if args.host else None,
            table_path=args.table,
            local_host=args.local_host,
            dry_run=args.dry_run,
        )
    )
    for line in result.lines():
        print(line, flush=True)
    if result.refusal:
        print(
            f"declared-public-keys-install refused: {result.refusal}", file=sys.stderr
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
