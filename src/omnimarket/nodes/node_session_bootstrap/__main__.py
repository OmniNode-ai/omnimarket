# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_session_bootstrap.

Usage:
    python -m omnimarket.nodes.node_session_bootstrap --dry-run

Outputs JSON to stdout: ModelBootstrapResult model.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime

from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)
from omnimarket.nodes.node_session_bootstrap.models.model_overnight_contract import (
    ModelOvernightContract,
)


def main() -> None:
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Run the overnight session bootstrap.")
    parser.add_argument(
        "--session-id",
        type=str,
        default=str(uuid.uuid4()),
        help="Session UUID (default: generated)",
    )
    parser.add_argument(
        "--session-label",
        type=str,
        default=f"{today} overnight",
        help="Human-readable session label",
    )
    parser.add_argument(
        "--phases-expected",
        type=str,
        default="build_loop,merge_sweep,platform_readiness",
        help="Comma-separated expected phases",
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
        type=str,
        default=".onex_state",
        help="Base path for contract snapshot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Skip filesystem writes",
    )

    args = parser.parse_args()

    phases = [p.strip() for p in args.phases_expected.split(",") if p.strip()]

    contract = ModelOvernightContract(
        session_id=args.session_id,
        session_label=args.session_label,
        phases_expected=phases,
        max_cycles=args.max_cycles,
        cost_ceiling_usd=args.cost_ceiling,
        started_at=datetime.now(tz=UTC),
    )

    command = ModelBootstrapCommand(
        session_id=args.session_id,
        contract=contract,
        state_dir=os.path.abspath(args.state_dir),
        dry_run=args.dry_run,
    )

    handler = HandlerSessionBootstrap()
    result = handler.handle(command)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
