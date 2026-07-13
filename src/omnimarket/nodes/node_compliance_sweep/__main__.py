# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_compliance_sweep.

Usage:
    python -m omnimarket.nodes.node_compliance_sweep \
        --repos omnibase_infra,omniintelligence \
        --dry-run

    # CI gate usage (OMN-14541): no $OMNI_HOME needed — the checkout root is
    # passed explicitly so the census is collected by real harness code, not
    # an operator-typed repo name.
    python -m omnimarket.nodes.node_compliance_sweep --target-dirs .

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
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Handler contract compliance sweep across repos."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo names, resolved against $OMNI_HOME (default: all handler repos)",
    )
    parser.add_argument(
        "--target-dirs",
        default="",
        help=(
            "Comma-separated absolute (or CWD-relative) directory paths to "
            "scan directly — bypasses $OMNI_HOME resolution entirely. Use "
            "this for a single-repo CI gate (OMN-14541)."
        ),
    )
    parser.add_argument(
        "--checks",
        default="",
        help=(
            "Comma-separated check IDs to run (default: all). "
            "Checks: hardcoded-topics,undeclared-transport,missing-routing,logic-in-node"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — no ticket creation",
    )

    args = parser.parse_args()

    checks = [c.strip() for c in args.checks.split(",") if c.strip()] or None
    target_dirs = [t.strip() for t in args.target_dirs.split(",") if t.strip()]

    if target_dirs:
        request = ComplianceSweepRequest(
            target_dirs=target_dirs,
            checks=checks,
            dry_run=args.dry_run,
        )
    else:
        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            _log.error(
                "OMNI_HOME is not set and --target-dirs was not given — "
                "cannot resolve repo directories"
            )
            sys.exit(1)
        repos = [r.strip() for r in args.repos.split(",") if r.strip()]
        request = ComplianceSweepRequest(
            repos=repos,
            checks=checks,
            dry_run=args.dry_run,
        )

    handler = NodeComplianceSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status != "compliant":
        if result.status == "error":
            _log.error("compliance sweep refused a verdict: %s", result.scan_error)
        sys.exit(1)


if __name__ == "__main__":
    main()
