# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_pipeline_fill.

Runs one RSD-driven pipeline fill cycle. Queries Linear for unstarted
active-sprint tickets, scores them, and dispatches the top-N to the
ticket pipeline. Outputs result JSON to stdout.

Usage:
    python -m omnimarket.nodes.node_pipeline_fill
    python -m omnimarket.nodes.node_pipeline_fill --dry-run
    python -m omnimarket.nodes.node_pipeline_fill --top-n 3 --wave-cap 10 --min-score 0.2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import uuid4

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
)
from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_command import (
    ModelPipelineFillCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run one RSD-driven pipeline fill cycle."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Score and rank tickets without dispatching.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        metavar="N",
        help="Maximum tickets to dispatch per cycle (default: 5).",
    )
    parser.add_argument(
        "--wave-cap",
        type=int,
        default=5,
        metavar="N",
        help="Maximum allowed in-flight dispatches (default: 5).",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.1,
        metavar="F",
        help="Minimum RSD score to dispatch (default: 0.1).",
    )
    parser.add_argument(
        "--state-dir",
        default=".onex_state/pipeline-fill",
        metavar="PATH",
        help="Directory for dispatch state files (default: .onex_state/pipeline-fill).",
    )

    args = parser.parse_args()

    command = ModelPipelineFillCommand(
        correlation_id=uuid4(),
        top_n=args.top_n,
        wave_cap=args.wave_cap,
        min_score=args.min_score,
        dry_run=args.dry_run,
        state_dir=args.state_dir,
    )

    handler = HandlerPipelineFill()

    try:
        result = asyncio.run(handler.handle(command))
    except Exception as exc:
        _log.error("pipeline_fill: fatal error: %s", exc)
        sys.exit(1)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
