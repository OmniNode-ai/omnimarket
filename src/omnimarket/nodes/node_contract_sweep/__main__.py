# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_contract_sweep.

Usage:
    python -m omnimarket.nodes.node_contract_sweep [--repos REPO,...] [--dry-run]

Outputs JSON to stdout: ContractSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    NodeContractSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Contract compliance sweep.")
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names to scan (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report violations without creating tickets",
    )
    args = parser.parse_args()

    repos = (
        [r.strip() for r in args.repos.split(",") if r.strip()] if args.repos else []
    )
    request = ContractSweepRequest(repos=repos, dry_run=args.dry_run)

    handler = NodeContractSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.violations:
        sys.exit(1)


if __name__ == "__main__":
    main()
