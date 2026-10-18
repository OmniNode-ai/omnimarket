# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_retention_cleanup.

Usage:
    python -m omnimarket.nodes.node_retention_cleanup [--dry-run] [--db-url URL]
        [--files [--only NAME ...] [--archive-root DIR]]

``--files`` also retires the contract's file_retention paths on this host.

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
    parser.add_argument(
        "--files",
        action="store_true",
        default=False,
        help="Also retire the contract's file_retention paths on this host",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=[],
        help="With --files, run only these file entries (by name)",
    )
    parser.add_argument(
        "--archive-root",
        default="",
        help="Archive tree for archive entries (default: the contract's archive_root_env)",
    )
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("OMNIDASH_ANALYTICS_DB_URL", "")
    request = RetentionCleanupRequest(
        dry_run=args.dry_run,
        db_url=db_url,
        include_files=args.files,
        file_only=tuple(args.only),
        file_archive_root=args.archive_root,
    )

    handler = NodeRetentionCleanup()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status.value == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
