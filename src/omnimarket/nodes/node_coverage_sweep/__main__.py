# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_coverage_sweep.

Scans Python repos for test coverage gaps below a configurable threshold.
Reads coverage.json files from repo directories.

Usage:
    python -m omnimarket.nodes.node_coverage_sweep \
        --repos omniclaude,omnibase_core \
        --target-pct 50 \
        --dry-run

Outputs JSON to stdout: CoverageSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    CoverageSweepRequest,
    NodeCoverageSweep,
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
    omni_home = os.environ.get("OMNI_HOME") or str(Path.home() / "omni_home")
    if not os.environ.get("OMNI_HOME"):
        _log.warning("OMNI_HOME not set; falling back to %s", omni_home)

    parser = argparse.ArgumentParser(
        description="Measure test coverage across Python repos, flag modules below threshold."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names to scan (default: all supported repos)",
    )
    parser.add_argument(
        "--target-pct",
        type=float,
        default=50.0,
        help="Coverage target percentage (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no ticket creation",
    )
    parser.add_argument(
        "--recently-changed",
        default="",
        help="Comma-separated module paths considered recently changed (for priority)",
    )

    args = parser.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS
    recently_changed = [
        m.strip() for m in args.recently_changed.split(",") if m.strip()
    ]

    target_dirs = _resolve_repo_dirs(repos, omni_home)
    if not target_dirs:
        _log.error("no valid repo directories resolved")
        sys.exit(1)

    request = CoverageSweepRequest(
        target_dirs=target_dirs,
        target_pct=args.target_pct,
        recently_changed_modules=recently_changed,
        dry_run=args.dry_run,
    )

    handler = NodeCoverageSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("clean",):
        sys.exit(1)


if __name__ == "__main__":
    main()
