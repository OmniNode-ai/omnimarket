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
from uuid import uuid4

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


def main() -> None:
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

    args = parser.parse_args()

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

    sys.stdout.write(report.model_dump_json(indent=2) + "\n")

    if report.stop_reason == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
