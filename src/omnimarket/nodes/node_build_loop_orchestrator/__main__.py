# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_build_loop_orchestrator.

Runs the autonomous build loop: CLOSING_OUT -> VERIFYING -> FILLING ->
CLASSIFYING -> BUILDING -> COMPLETE.

Usage:
    python -m omnimarket.nodes.node_build_loop_orchestrator \
        --max-cycles 1 \
        --skip-closeout \
        --dry-run

Outputs JSON to stdout: ModelOrchestratorResult model.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run the autonomous build loop (close-out -> verify -> fill -> classify -> build)."
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="Maximum number of build loop cycles to run (default: 1)",
    )
    parser.add_argument(
        "--skip-closeout",
        action="store_true",
        default=False,
        help="Skip the CLOSING_OUT phase",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate the full loop without side effects",
    )
    parser.add_argument(
        "--max-tickets",
        type=int,
        default=5,
        help="Maximum tickets to dispatch per fill cycle (default: 5)",
    )
    parser.add_argument(
        "--mode",
        default="build",
        help="Execution mode: build, close_out, full, observe (default: build)",
    )

    args = parser.parse_args()

    command = ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=args.max_cycles,
        mode=args.mode,
        skip_closeout=args.skip_closeout,
        max_tickets=args.max_tickets,
        dry_run=args.dry_run,
        requested_at=datetime.now(UTC),
    )

    handler = HandlerBuildLoopOrchestrator()
    result = asyncio.run(handler.handle(command))

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.cycles_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
