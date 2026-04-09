# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_redeploy.

NOTE: This node is currently a structural placeholder. Business logic lives
in the omniclaude skill (onex:redeploy) and will be migrated here in a
follow-up ticket. This CLI parses args and emits the start command as JSON
for contract verification purposes.

Usage:
    python -m omnimarket.nodes.node_redeploy
    python -m omnimarket.nodes.node_redeploy --versions omniintelligence=0.8.0
    python -m omnimarket.nodes.node_redeploy --dry-run

Outputs JSON to stdout: ModelRedeployStartCommand model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from uuid import uuid4

from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    ModelRedeployStartCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Full post-release runtime redeploy. "
            "NOTE: handler is a structural placeholder — logic migrates in a follow-up."
        )
    )
    parser.add_argument(
        "--versions",
        default="",
        help=(
            "Comma-separated plugin version pins: pkg=version,pkg2=version2. "
            "If omitted, auto-detected from latest git tags."
        ),
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        default=False,
        help="Skip SYNC phase (bare clones already current)",
    )
    parser.add_argument(
        "--skip-dockerfile-update",
        action="store_true",
        default=False,
        help="Skip PIN_UPDATE phase",
    )
    parser.add_argument(
        "--skip-infisical",
        action="store_true",
        default=False,
        help="Skip INFISICAL phase",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        default=False,
        help="Skip to VERIFY phase only (assumes runtime already running)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print step commands without execution",
    )
    parser.add_argument(
        "--resume",
        default="",
        help="Resume from first non-completed phase by run_id",
    )

    args = parser.parse_args()

    command = ModelRedeployStartCommand(
        correlation_id=str(uuid4()),
        versions=args.versions,
        skip_sync=args.skip_sync,
        skip_dockerfile_update=args.skip_dockerfile_update,
        skip_infisical=args.skip_infisical,
        verify_only=args.verify_only,
        dry_run=args.dry_run,
        resume=args.resume,
    )

    sys.stdout.write(command.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
