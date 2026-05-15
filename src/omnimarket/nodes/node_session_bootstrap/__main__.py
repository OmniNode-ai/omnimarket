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

from omnimarket.cli.args import (
    add_output_args,
    report_output_requested,
    resolve_output_config,
)
from omnimarket.cli.output.registry import resolve_handler
from omnimarket.cli.reporting import (
    build_report_from_model_result,
    load_contract_metadata,
)
from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)
from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)


def main() -> None:
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Run the session bootstrap.")
    parser.add_argument(
        "--session-id",
        type=str,
        default=str(uuid.uuid4()),
        help="Session UUID (default: generated)",
    )
    parser.add_argument(
        "--session-label",
        type=str,
        default=f"{today} session",
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
    parser.add_argument(
        "--enable-cron-shim",
        action="store_true",
        default=False,
        help="Best-effort Claude cron compatibility shim; launchd remains primary",
    )
    add_output_args(parser)

    args = parser.parse_args()
    output_config = resolve_output_config(args)

    phases = [p.strip() for p in args.phases_expected.split(",") if p.strip()]

    contract = ModelSessionContract(
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
        enable_cron_shim=args.enable_cron_shim,
    )

    handler = HandlerSessionBootstrap()
    result = handler.handle(command)
    if not report_output_requested():
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        if result.status == "failed":
            sys.exit(1)
        return

    contract_name, contract_version, terminal_event = load_contract_metadata(
        "omnimarket.nodes.node_session_bootstrap"
    )
    cli_report = build_report_from_model_result(
        result,
        skill_name="session_bootstrap",
        node_name="node_session_bootstrap",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dry_run" if args.dry_run else "execute",
        input_summary={
            "session_id": args.session_id,
            "session_label": args.session_label,
            "phases_expected": phases,
            "max_cycles": args.max_cycles,
            "cost_ceiling_usd": args.cost_ceiling,
            "state_dir": os.path.abspath(args.state_dir),
            "dry_run": args.dry_run,
        },
        output_config=output_config,
    )
    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")

    if result.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
