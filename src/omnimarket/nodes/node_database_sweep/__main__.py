# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_database_sweep.

Usage:
    python -m omnimarket.nodes.node_database_sweep
    python -m omnimarket.nodes.node_database_sweep --dry-run
    python -m omnimarket.nodes.node_database_sweep --table agent_routing_decisions
    python -m omnimarket.nodes.node_database_sweep --staleness-threshold 48

Outputs JSON to stdout: DatabaseSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    DatabaseSweepRequest,
    NodeDatabaseSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Projection table health and migration tracking across ONEX databases."
    )
    parser.add_argument(
        "--omni-home",
        default="",
        help="Root path of omni_home (default: $OMNI_HOME env var)",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Check a single table only (default: all tables in omnidash_analytics)",
    )
    parser.add_argument(
        "--staleness-threshold",
        type=int,
        default=24,
        help="Hours before data is considered stale (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no ticket creation",
    )

    args = parser.parse_args()

    request = DatabaseSweepRequest(
        omni_home=args.omni_home,
        table=args.table,
        staleness_threshold_hours=args.staleness_threshold,
        dry_run=args.dry_run,
    )

    handler = NodeDatabaseSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("healthy",):
        sys.exit(1)


if __name__ == "__main__":
    main()
