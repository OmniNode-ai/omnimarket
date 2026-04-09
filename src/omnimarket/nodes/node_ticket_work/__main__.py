# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_ticket_work.

NOTE: This node is currently a structural placeholder. Business logic lives
in the omniclaude skill (onex:ticket_work) and will be migrated here in a
follow-up ticket. This CLI parses args and emits the start command as JSON
for contract verification purposes.

Usage:
    python -m omnimarket.nodes.node_ticket_work OMN-1234
    python -m omnimarket.nodes.node_ticket_work OMN-1234 --autonomous

Outputs JSON to stdout: ModelTicketWorkStartCommand model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from uuid import uuid4

from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    ModelTicketWorkStartCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Contract-driven ticket execution. "
            "NOTE: handler is a structural placeholder — logic migrates in a follow-up."
        )
    )
    parser.add_argument(
        "ticket_id",
        help="Linear ticket ID (e.g., OMN-1234)",
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        default=False,
        help="Skip human gates; proceed through all phases unattended",
    )
    parser.add_argument(
        "--skip-to",
        default="",
        help="Resume from specified phase",
    )

    args = parser.parse_args()

    command = ModelTicketWorkStartCommand(
        correlation_id=str(uuid4()),
        ticket_id=args.ticket_id,
        autonomous=args.autonomous,
        skip_to=args.skip_to,
    )

    sys.stdout.write(command.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
