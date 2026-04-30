# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_ticket_pipeline.

Runs the safe ticket-pipeline execution slice for a given Linear ticket and
outputs a parseable JSON report. PRE_FLIGHT and compile-only IMPLEMENT are wired;
later side-effect phases stop as blocked/not_implemented.

Usage:
    python -m omnimarket.nodes.node_ticket_pipeline OMN-1234
    python -m omnimarket.nodes.node_ticket_pipeline OMN-1234 --dry-run
    python -m omnimarket.nodes.node_ticket_pipeline OMN-1234 --skip-to ci_watch

Outputs JSON to stdout: ModelPipelineExecutionReport model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from importlib import resources
from uuid import uuid4

import yaml

from omnimarket.cli.args import add_output_args, resolve_output_config
from omnimarket.cli.output.registry import resolve_handler
from omnimarket.cli.reporting import build_report_from_pipeline_result
from omnimarket.nodes.node_ticket_pipeline.handlers.handler_ticket_pipeline import (
    HandlerTicketPipeline,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_start_command import (
    ModelPipelineStartCommand,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EXECUTABLE_PHASE_ORDER,
)

_log = logging.getLogger(__name__)


def _contract_metadata() -> tuple[str, str, str]:
    contract_path = resources.files("omnimarket.nodes.node_ticket_pipeline").joinpath(
        "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    version = raw["contract_version"]
    contract_version = f"{version['major']}.{version['minor']}.{version['patch']}"
    return str(raw["name"]), contract_version, str(raw["terminal_event"])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Initialize the ticket pipeline FSM for a Linear ticket."
    )
    parser.add_argument(
        "ticket_id",
        help="Linear ticket ID (e.g., OMN-1234)",
    )
    parser.add_argument(
        "--skip-to",
        default=None,
        choices=[phase.value for phase in EXECUTABLE_PHASE_ORDER],
        help=(
            "Resume from specified phase: pre_flight|implement|local_review|"
            "create_pr|test_iterate|ci_watch|pr_review|auto_merge"
        ),
    )
    parser.add_argument(
        "--skip-test-iterate",
        action="store_true",
        default=False,
        help="Skip the test-iterate phase",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log phase decisions without side effects",
    )
    add_output_args(parser)

    args = parser.parse_args(argv)
    output_config = resolve_output_config(args)
    logging.getLogger().setLevel(args.log_level.upper())

    command = ModelPipelineStartCommand(
        correlation_id=uuid4(),
        ticket_id=args.ticket_id,
        skip_test_iterate=args.skip_test_iterate,
        dry_run=args.dry_run,
        skip_to=args.skip_to,
        requested_at=datetime.now(UTC),
    )

    handler = HandlerTicketPipeline()
    report = handler.run_executable_pipeline(command)
    contract_name, contract_version, terminal_event = _contract_metadata()
    cli_report = build_report_from_pipeline_result(
        report,
        skill_name="ticket_pipeline",
        node_name="node_ticket_pipeline",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dry_run" if args.dry_run else "execute",
        input_summary={
            "ticket_id": args.ticket_id,
            "skip_to": args.skip_to,
            "skip_test_iterate": args.skip_test_iterate,
            "dry_run": args.dry_run,
        },
        output_config=output_config,
    )

    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")

    if report.stop_reason == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
