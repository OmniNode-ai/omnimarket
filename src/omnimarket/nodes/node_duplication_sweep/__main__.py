# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_duplication_sweep.

Usage:
    python -m omnimarket.nodes.node_duplication_sweep
    python -m omnimarket.nodes.node_duplication_sweep --check D1,D2
    python -m omnimarket.nodes.node_duplication_sweep --omni-home /path/to/omni_home

Outputs JSON to stdout: DuplicationSweepResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
    DuplicationSweepRequest,
    NodeDuplicationSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Detect duplicate definitions across ONEX repos."
    )
    parser.add_argument(
        "--omni-home",
        default="",
        help="Root path of omni_home (default: $OMNI_HOME env var)",
    )
    parser.add_argument(
        "--check",
        default="",
        help="Comma-separated check IDs to run: D1,D2,D3,D4 (default: all)",
    )

    args = parser.parse_args()
    checks = [c.strip() for c in args.check.split(",") if c.strip()] or None

    request = DuplicationSweepRequest(
        omni_home=args.omni_home,
        checks=checks,
    )

    handler = NodeDuplicationSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.overall_status == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
