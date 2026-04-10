# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_plan_to_tickets.

Reads a plan markdown file, parses ## Task N: / ## Phase N: sections into
structured ticket entries, and validates dependencies and cycle detection.
The actual Linear API calls (epic resolution, ticket creation) are performed
by the skill wrapper in omniclaude.

Usage:
    python -m omnimarket.nodes.node_plan_to_tickets path/to/plan.md
    python -m omnimarket.nodes.node_plan_to_tickets path/to/plan.md --dry-run

Outputs JSON to stdout: ModelPlanToTicketsResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from omnimarket.nodes.node_plan_to_tickets.handlers.handler_plan_to_tickets import (
    HandlerPlanToTickets,
    ModelPlanToTicketsRequest,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Parse a plan markdown file into structured ticket entries."
    )
    parser.add_argument(
        "plan_file",
        help="Path to plan markdown file (must contain ## Task N: or ## Phase N: sections).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Parse and validate without performing any Linear API calls.",
    )

    args = parser.parse_args()

    plan_path = Path(args.plan_file).expanduser().resolve()
    if not plan_path.exists():
        _log.error("Plan file not found: %s", plan_path)
        sys.exit(1)

    plan_content = plan_path.read_text(encoding="utf-8")

    request = ModelPlanToTicketsRequest(
        plan_content=plan_content,
        dry_run=args.dry_run,
    )

    handler = HandlerPlanToTickets()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
