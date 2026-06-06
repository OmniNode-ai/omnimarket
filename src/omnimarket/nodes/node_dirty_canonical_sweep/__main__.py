# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dirty_canonical_sweep."""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_dirty_canonical_sweep.handlers import (
    HandlerDirtyCanonicalSweep,
)
from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Detect dirty canonical omni_home repos and auto-ship to worktrees + PRs."
    )
    parser.add_argument(
        "--omni-home",
        default=None,
        help="Override OMNI_HOME path.",
    )
    parser.add_argument(
        "--worktrees-root",
        default=None,
        help="Override worktrees root path.",
    )
    parser.add_argument(
        "--repos",
        nargs="*",
        default=None,
        help="Explicit repo names to check (default: all repos under omni_home).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and report dirty repos without moving files or creating PRs.",
    )
    parser.add_argument(
        "--pr-label",
        default="auto-ship",
        help="GitHub label to attach to auto-shipped PRs.",
    )
    parser.add_argument(
        "--base-branch",
        default="dev",
        help="Git branch to create rescue worktrees from and target PRs against.",
    )

    args = parser.parse_args()
    command = ModelDirtyCanonicalSweepCommand(
        omni_home=args.omni_home,
        worktrees_root=args.worktrees_root,
        repos=args.repos,
        dry_run=args.dry_run,
        pr_label=args.pr_label,
        base_branch=args.base_branch,
    )
    result = HandlerDirtyCanonicalSweep().handle(command)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
