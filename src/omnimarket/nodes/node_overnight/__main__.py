# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_overnight.

Usage:
    python -m omnimarket.nodes.node_overnight \
        --dry-run \
        --skip-build-loop

    python -m omnimarket.nodes.node_overnight \
        --contract-file path/to/overnight-contract.yaml \
        --dry-run

Outputs JSON to stdout: ModelOvernightResult model.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import yaml
from omnibase_compat.overseer.model_overnight_contract import ModelOvernightContract

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
    parser.add_argument(
        "--contract-file",
        type=str,
        default=None,
        help=(
            "Path to a ModelOvernightContract YAML file. When set, the contract "
            "is loaded and attached to the overnight command, enabling cost "
            "ceiling and halt_on_failure enforcement."
        ),
    )

    args = parser.parse_args()

    overnight_contract: ModelOvernightContract | None = None
    if args.contract_file:
        contract_path = Path(args.contract_file).expanduser().resolve()
        if not contract_path.exists():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")
        data = yaml.safe_load(contract_path.read_text())
        overnight_contract = ModelOvernightContract.model_validate(data)

    command = ModelOvernightCommand(
        correlation_id=str(uuid.uuid4()),
        max_cycles=args.max_cycles,
        skip_build_loop=args.skip_build_loop,
        skip_merge_sweep=args.skip_merge_sweep,
        dry_run=args.dry_run,
        overnight_contract=overnight_contract,
    )

    handler = HandlerOvernight()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.session_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
