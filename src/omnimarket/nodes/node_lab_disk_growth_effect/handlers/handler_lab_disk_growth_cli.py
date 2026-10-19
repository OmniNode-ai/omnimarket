# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Expose the same disk growth passes to maintenance timers and manual runs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..models import ModelLabPluginSnapshotEvictRequest, ModelLabUvCacheColocateRequest
from .handler_lab_plugin_snapshot_evict import (
    HandlerLabPluginSnapshotEvict,
)
from .handler_lab_uv_cache_colocate import (
    HandlerLabUvCacheColocate,
    default_uv_config,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_lab_disk_growth_effect"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plugins = commands.add_parser("plugin-snapshots")
    plugins.add_argument("--plugins-dir", type=Path, required=True)
    plugins.add_argument("--keep-newest", type=int, default=3)
    plugins.add_argument("--dry-run", action="store_true")
    uv = commands.add_parser("uv-cache")
    uv.add_argument("--worktrees-root", type=Path, required=True)
    uv.add_argument("--cache-dir", type=Path, required=True)
    uv.add_argument("--uv-config", type=Path, default=default_uv_config())
    uv.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plugin-snapshots":
        snapshots = HandlerLabPluginSnapshotEvict().handle(
            ModelLabPluginSnapshotEvictRequest(
                plugins_dir=args.plugins_dir.expanduser(),
                keep_newest=args.keep_newest,
                dry_run=args.dry_run,
            )
        )
        sys.stdout.write(snapshots.line() + "\n")
        for error in snapshots.errors:
            sys.stderr.write(f"{error}\n")
        return int(snapshots.status == "refused" or bool(snapshots.errors))
    cache = HandlerLabUvCacheColocate().handle(
        ModelLabUvCacheColocateRequest(
            worktrees_root=args.worktrees_root.expanduser(),
            cache_dir=args.cache_dir.expanduser(),
            uv_config=args.uv_config.expanduser(),
            dry_run=args.dry_run,
        )
    )
    sys.stdout.write(cache.line() + "\n")
    for override in cache.env_overrides:
        sys.stderr.write(f"{override}\n")
    if cache.status == "refused":
        sys.stderr.write(f"{cache.reason}\n")
    return int(cache.status == "refused")


if __name__ == "__main__":
    raise SystemExit(main())
