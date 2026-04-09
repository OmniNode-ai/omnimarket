# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_retention_cleanup.

Usage:
    python -m omnimarket.nodes.node_retention_cleanup [--dry-run] [--db-url URL]

Outputs JSON to stdout: RetentionCleanupResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Projection table retention cleanup.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Estimate rows to delete without executing DELETE statements",
    )
    parser.add_argument(
        "--db-url",
        default="",
        help="PostgreSQL connection URL (default: OMNIDASH_ANALYTICS_DB_URL env var)",
    )
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("OMNIDASH_ANALYTICS_DB_URL", "")
    request = RetentionCleanupRequest(dry_run=args.dry_run, db_url=db_url)

    handler = NodeRetentionCleanup()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status.value == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
