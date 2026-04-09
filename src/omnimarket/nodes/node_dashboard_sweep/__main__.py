# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dashboard_sweep.

Classifies dashboard pages and triages into problem domains.
Page data is passed via --pages JSON array.

Usage:
    python -m omnimarket.nodes.node_dashboard_sweep \
        --pages '[{"route": "/agents", "status_code": 200, "visible_text": "No agents"}]' \
        --max-iterations 3 \
        --dry-run

Outputs JSON to stdout: DashboardSweepResult model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    DashboardSweepRequest,
    ModelPageInput,
    NodeDashboardSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Classify dashboard pages and triage problem domains."
    )
    parser.add_argument(
        "--pages",
        default="[]",
        help=(
            "JSON array of page objects with keys: route, status_code, "
            "visible_text, console_errors (default: empty)"
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum fix-reaudit iterations (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Classify and triage only — no fix dispatch",
    )

    args = parser.parse_args()

    try:
        raw_pages: list[dict[str, object]] = json.loads(args.pages)
    except json.JSONDecodeError as exc:
        _log.error("invalid --pages JSON: %s", exc)
        sys.exit(1)

    pages = [ModelPageInput.model_validate(p) for p in raw_pages]

    request = DashboardSweepRequest(
        pages=pages,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
    )

    handler = NodeDashboardSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("clean",):
        sys.exit(1)


if __name__ == "__main__":
    main()
