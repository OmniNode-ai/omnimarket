# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_aislop_sweep.

Usage:
    python -m omnimarket.nodes.node_aislop_sweep \
        --repos omniclaude,omnibase_core \
        --checks prohibited-patterns,hardcoded-topics \
        --dry-run

Outputs JSON to stdout: AislopSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)

_log = logging.getLogger(__name__)

_DEFAULT_REPOS = [
    "omniclaude",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omniintelligence",
    "omnimemory",
    "onex_change_control",
    "omnibase_compat",
]


def _resolve_repo_dirs(repos: list[str], omni_home: str) -> list[str]:
    """Resolve repo names to absolute paths under omni_home."""
    root = Path(omni_home)
    resolved: list[str] = []
    for repo in repos:
        p = root / repo
        if p.is_dir():
            resolved.append(str(p))
        else:
            _log.warning("repo dir not found: %s", p)
    return resolved


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME", "/Volumes/PRO-G40/Code/omni_home")

    parser = argparse.ArgumentParser(
        description="Detect AI-generated quality anti-patterns across repos."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names (default: all supported repos)",
    )
    parser.add_argument(
        "--checks",
        default="",
        help=(
            "Comma-separated check categories: "
            "phantom-callables,compat-shims,prohibited-patterns,"
            "hardcoded-topics,todo-fixme,todo-stale,empty-impls "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no tickets, no fixes",
    )
    parser.add_argument(
        "--severity-threshold",
        default="WARNING",
        choices=["WARNING", "ERROR", "CRITICAL"],
        help="Minimum severity to act on (default: WARNING)",
    )

    args = parser.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS
    checks = [c.strip() for c in args.checks.split(",") if c.strip()] or None

    target_dirs = _resolve_repo_dirs(repos, omni_home)
    if not target_dirs:
        _log.error("no valid repo directories resolved")
        sys.exit(1)

    request = AislopSweepRequest(
        target_dirs=target_dirs,
        checks=checks,
        dry_run=args.dry_run,
        severity_threshold=args.severity_threshold,
    )

    handler = NodeAislopSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    # Exit non-zero when findings exist
    if result.status not in ("clean",):
        sys.exit(1)


if __name__ == "__main__":
    main()
