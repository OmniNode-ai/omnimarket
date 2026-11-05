# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host CLI adapter for the retention cleanup node's typed handlers."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_retention_cleanup.handlers.handler_lane_worktree_retention import (
    HandlerLaneWorktreeRetention,
    load_lane_worktree_policy,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)
from omnimarket.nodes.node_retention_cleanup.models.model_lane_worktree_retention import (
    ModelLaneWorktreeRetentionRequest,
)


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
        "--lane-worktrees",
        action="store_true",
        default=False,
        help="Run only the lane worktree pass over the contract's runs_dir",
    )
    parser.add_argument(
        "--runs-dir",
        default="",
        help="With --lane-worktrees, the runs directory (default: the contract's runs_dir)",
    )
    args = parser.parse_args()

    if args.lane_worktrees:
        sys.exit(_lane_worktrees(args.runs_dir, args.dry_run))

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


def _lane_worktrees(runs_dir: str, dry_run: bool) -> int:
    policy = load_lane_worktree_policy()
    lane = HandlerLaneWorktreeRetention().handle(
        ModelLaneWorktreeRetentionRequest(
            runs_dir=Path(runs_dir or policy.runs_dir).expanduser(),
            now=dt.datetime.now(dt.UTC),
            idle_days=policy.idle_days,
            ctx_keep_days=policy.ctx_keep_days,
            dry_run=dry_run,
        )
    )
    sys.stdout.write(lane.line() + "\n" + lane.model_dump_json(indent=2) + "\n")
    for error in lane.errors:
        sys.stderr.write(f"{error}\n")
    if lane.status == "skipped":
        sys.stderr.write(f"{lane.reason}\n")
    return int(lane.status == "skipped" or bool(lane.errors))
