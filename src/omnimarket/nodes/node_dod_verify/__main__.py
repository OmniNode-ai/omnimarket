# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dod_verify.

Runs DoD evidence verification for a Linear ticket and outputs the result as JSON.

Usage:
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --contract-path /path/to/contract.yaml
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import UTC, datetime

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run DoD evidence verification for a Linear ticket."
    )
    parser.add_argument(
        "--ticket-id",
        type=str,
        required=True,
        help="Linear ticket ID (e.g. OMN-1234)",
    )
    parser.add_argument(
        "--contract-path",
        type=str,
        default=None,
        help="Override path to contract YAML (default: auto-discovered from ticket ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run verification checks but do not emit events",
    )
    parser.add_argument(
        "--correlation-id",
        type=uuid.UUID,
        default=None,
        help="Correlation ID (UUID) for this run (default: auto-generated)",
    )

    args = parser.parse_args()

    correlation_id = (
        args.correlation_id if args.correlation_id is not None else uuid.uuid4()
    )

    command = ModelDodVerifyStartCommand(
        correlation_id=correlation_id,
        ticket_id=args.ticket_id,
        contract_path=args.contract_path,
        dry_run=args.dry_run,
        requested_at=datetime.now(tz=UTC),
    )

    handler = HandlerDodVerify()
    state, _event = handler.run_verification(command)

    sys.stdout.write(state.model_dump_json(indent=2) + "\n")

    if state.status == EnumDodVerifyStatus.FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
