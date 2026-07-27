# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_create_ticket.

Validates ticket parameters, detects seam signals, generates the structured
description body, and creates the Linear ticket via the GraphQL API
(``HandlerCreateTicket`` resolves ``LINEAR_API_KEY`` and performs the
``issueCreate`` call directly — see OMN-14547). A non-dry-run request that
does not resolve to a real ``ticket_id`` raises rather than reporting a fake
``status="created"``; this CLI catches that and reports ``status="error"``.

Usage:
    python -m omnimarket.nodes.node_create_ticket --title "Add rate limiting"
    python -m omnimarket.nodes.node_create_ticket --title "Add rate limiting" --repo omnibase_core --parent OMN-1800
    python -m omnimarket.nodes.node_create_ticket --title "Deploy pipeline" --blocked-by OMN-1801,OMN-1802 --dry-run

Outputs JSON to stdout: ModelCreateTicketResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Validate and prepare a Linear ticket for creation."
    )
    parser.add_argument("--title", default="", help="Ticket title.")
    parser.add_argument("--description", default="", help="Ticket description body.")
    parser.add_argument(
        "--repo",
        default="",
        help="Primary repository label (e.g. omniclaude, omnibase_core).",
    )
    parser.add_argument(
        "--parent",
        default="",
        help="Parent ticket ID for epic relationship (OMN-XXXX).",
    )
    parser.add_argument(
        "--blocked-by",
        default="",
        dest="blocked_by",
        help="Comma-separated blocking ticket IDs (OMN-XXXX,...).",
    )
    parser.add_argument(
        "--team",
        default="Omninode",
        help="Linear team name (default: Omninode).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and report without issuing any Linear API calls.",
    )

    args = parser.parse_args()

    if not args.title:
        parser.error("--title is required")

    blocked_by: list[str] = (
        [b.strip() for b in args.blocked_by.split(",") if b.strip()]
        if args.blocked_by
        else []
    )

    request = ModelCreateTicketRequest(
        title=args.title,
        description=args.description,
        repo=args.repo or None,
        parent=args.parent or None,
        blocked_by=blocked_by,
        team=args.team,
        dry_run=args.dry_run,
    )

    handler = HandlerCreateTicket()
    try:
        result = handler.handle(request)
    except RuntimeError as exc:
        # Fail-closed guard (OMN-14547) or secret-resolution failure — report
        # as a structured error rather than an uncaught traceback, preserving
        # this CLI's "always prints a ModelCreateTicketResult JSON" contract.
        result = ModelCreateTicketResult(
            status="error",
            title=request.title,
            team=request.team,
            validation_errors=[str(exc)],
            dry_run=request.dry_run,
        )

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
