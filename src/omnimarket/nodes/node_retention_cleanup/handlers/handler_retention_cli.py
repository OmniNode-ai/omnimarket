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
from pathlib import Path

from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)

from .handler_file_retention import load_file_policy

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
    parser.add_argument(
        "--omni-home",
        dest="registry_root",
        type=Path,
        help="Registry root supplied by the host installer",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Write last-run.json for the timer's status readback",
    )
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("OMNIDASH_ANALYTICS_DB_URL", "")
    request = RetentionCleanupRequest(
        dry_run=args.dry_run,
        db_url=db_url,
        include_files=args.files,
        file_only=tuple(args.only),
        file_archive_root=args.archive_root,
        file_entries=[
            entry.model_copy(
                update={
                    "path": entry.path.replace("$OMNI_HOME", str(args.registry_root))
                }
            )
            for entry in load_file_policy().entries
        ]
        if args.registry_root is not None and args.files
        else None,
    )

    handler = NodeRetentionCleanup()
    result = handler.handle(request)

    payload = result.model_dump_json(indent=2) + "\n"
    sys.stdout.write(payload)
    if args.state_dir is not None and not args.dry_run:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        pending = args.state_dir / "last-run.json.tmp"
        pending.write_text(payload, encoding="utf-8")
        pending.replace(args.state_dir / "last-run.json")

    if result.status.value == "error":
        sys.exit(1)
