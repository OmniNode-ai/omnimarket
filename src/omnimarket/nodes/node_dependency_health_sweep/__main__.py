# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dependency_health_sweep.

Usage:
    python -m omnimarket.nodes.node_dependency_health_sweep \
        --repo-roots src/ \
        --severity-threshold MAJOR \
        --dry-run

Outputs JSON to stdout: ModelDepHealthSweepResult model.
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
    HandlerDepHealthSweep,
)
from omnimarket.nodes.node_dependency_health_sweep.models import (
    ModelDepHealthSweepRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze dependency health across ONEX delegation pipeline repos."
    )
    parser.add_argument(
        "--repo-roots",
        action="append",
        default=[],
        dest="repo_roots",
        help="Repo root path to scan. Can be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run analysis without emitting events.",
    )
    parser.add_argument(
        "--severity-threshold",
        default="MAJOR",
        dest="severity_threshold",
        help="Minimum severity to report (CRITICAL, MAJOR, MINOR, INFO).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        dest="run_id",
        help="Stable run ID for idempotency. Generated if not supplied.",
    )
    parser.add_argument(
        "--baseline-path",
        default=None,
        dest="baseline_path",
        help="Path to baseline JSON for delta computation.",
    )
    args = parser.parse_args(argv)

    request = ModelDepHealthSweepRequest(
        repo_roots=args.repo_roots,
        severity_threshold=args.severity_threshold,
        dry_run=args.dry_run,
        run_id=args.run_id,
        baseline_path=args.baseline_path,
    )

    handler = HandlerDepHealthSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
