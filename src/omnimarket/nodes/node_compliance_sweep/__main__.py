# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_compliance_sweep.

Usage:
    python -m omnimarket.nodes.node_compliance_sweep \
        --repos omnibase_infra,omniintelligence \
        --dry-run

Outputs JSON to stdout: ComplianceSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    NodeComplianceSweep,
    resolve_target_dirs,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        _log.error("OMNI_HOME is not set — cannot resolve repo directories")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Handler contract compliance sweep across repos."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names (default: all handler repos)",
    )
    parser.add_argument(
        "--checks",
        default="",
        help=(
            "Comma-separated check IDs to run (default: all). "
            "Checks: topic-compliance,transport-compliance,handler-routing,logic-in-node"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no ticket creation",
    )

    args = parser.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    checks = [c.strip() for c in args.checks.split(",") if c.strip()] or None

    # Build the request with bare repo names; the handler shares the same
    # resolver so the CLI and the RuntimeLocal dispatch path scan identically
    # (OMN-13514). We resolve here too purely to fail fast on an empty target
    # set with a clear message before dispatching.
    request = ComplianceSweepRequest(
        repos=repos,
        checks=checks,
        dry_run=args.dry_run,
    )
    target_dirs = resolve_target_dirs(request, omni_home)
    if not target_dirs:
        _log.error("no valid repo directories resolved")
        sys.exit(1)

    handler = NodeComplianceSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status not in ("compliant",):
        sys.exit(1)


if __name__ == "__main__":
    main()
