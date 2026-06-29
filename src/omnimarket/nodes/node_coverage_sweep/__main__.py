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

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    CoverageSweepRequest,
    NodeCoverageSweep,
)
from omnimarket.nodes.sweep_scope import resolve_default_target_dirs

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        _log.error("OMNI_HOME is not set — cannot resolve repo directories")
        sys.exit(1)

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

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    recently_changed = [
        m.strip() for m in args.recently_changed.split(",") if m.strip()
    ]

    # Resolve here via the shared resolver purely to fail fast with a clear
    # message before dispatching; the handler shares the same resolver so the
    # CLI and RuntimeLocal dispatch paths scan identically (OMN-13538).
    target_dirs = resolve_default_target_dirs([], repos, omni_home)
    if not target_dirs:
        _log.error("no valid repo directories resolved")
        sys.exit(1)

    request = CoverageSweepRequest(
        repos=repos,
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
