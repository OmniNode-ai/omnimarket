# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_release.

NOTE: This node is currently a structural placeholder. Business logic lives
in the omniclaude skill (onex:release) and will be migrated here in a
follow-up ticket. This CLI parses args and emits the start command as JSON
for contract verification purposes.

Usage:
    python -m omnimarket.nodes.node_release omniclaude omnibase_core
    python -m omnimarket.nodes.node_release --all --bump patch --dry-run

Outputs JSON to stdout: ModelReleaseStartCommand model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from uuid import uuid4

from omnimarket.nodes.node_release.models.model_release_state import (
    ModelReleaseStartCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Org-wide coordinated release pipeline. "
            "NOTE: handler is a structural placeholder — logic migrates in a follow-up."
        )
    )
    parser.add_argument(
        "repos",
        nargs="*",
        help="Repo names to release (default: all repos in dependency graph)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Explicitly release all repos in dependency graph",
    )
    parser.add_argument(
        "--bump",
        default="",
        help="Override bump level for all repos: major | minor | patch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show plan table and exit without making changes",
    )
    parser.add_argument(
        "--resume",
        default="",
        help="Resume from a previously failed run by run_id",
    )
    parser.add_argument(
        "--skip-pypi-wait",
        action="store_true",
        default=False,
        help="Don't block on PyPI package availability after publish trigger",
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        default=False,
        help="(Deprecated — gate removed; kept for CLI compatibility)",
    )
    parser.add_argument(
        "--gate-attestation",
        default="",
        help="Pre-issued gate token for audit trail",
    )

    args = parser.parse_args()

    repos = args.repos if args.repos else []

    command = ModelReleaseStartCommand(
        correlation_id=str(uuid4()),
        repos=repos,
        bump=args.bump,
        dry_run=args.dry_run,
        resume=args.resume,
        skip_pypi_wait=args.skip_pypi_wait,
        autonomous=args.autonomous,
        gate_attestation=args.gate_attestation,
    )

    sys.stdout.write(command.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
