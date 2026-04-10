# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_bootstrap.

Usage:
    python -m omnimarket.nodes.node_session_bootstrap \\
        --session-id <uuid> \\
        --phases-expected build_loop,merge_sweep,platform_readiness \\
        --dry-run

Outputs JSON to stdout: ModelBootstrapResult model.
Exits 1 if bootstrap FAILED.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date

from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    EnumBootstrapStatus,
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the session bootstrap node.")
    parser.add_argument(
        "--session-id",
        default=str(uuid.uuid4()),
        help="Unique session ID (default: generated UUID)",
    )
    parser.add_argument(
        "--session-label",
        default=f"{date.today().isoformat()} overnight",
        help="Human-readable session label",
    )
    parser.add_argument(
        "--phases-expected",
        default="build_loop,merge_sweep,platform_readiness",
        help="Comma-separated list of expected phases",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum build loop cycles (0 = unlimited)",
    )
    parser.add_argument(
        "--cost-ceiling",
        type=float,
        default=10.0,
        help="Advisory cost ceiling in USD",
    )
    parser.add_argument(
        "--state-dir",
        default=".onex_state",
        help="Base path for contract snapshot (default: .onex_state)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate without writing to filesystem",
    )

    args = parser.parse_args()

    phases = [p.strip() for p in args.phases_expected.split(",") if p.strip()]
    state_dir = os.path.abspath(args.state_dir)

    contract: dict[str, object] = {
        "session_id": args.session_id,
        "session_label": args.session_label,
        "phases_expected": phases,
        "max_cycles": args.max_cycles,
        "cost_ceiling_usd": args.cost_ceiling,
        "halt_on_build_loop_failure": True,
        "dry_run": args.dry_run,
        "schema_version": "1.0",
    }

    command = ModelBootstrapCommand(
        session_id=args.session_id,
        contract=contract,
        state_dir=state_dir,
        dry_run=args.dry_run,
    )

    handler = HandlerSessionBootstrap()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status == EnumBootstrapStatus.FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
