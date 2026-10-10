# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_create_ticket: create, read, comment or transition a ticket.

A create runs the installed ticket-creation guard on the exact payload filed
(Backlog, no project, the description unchanged) and creates nothing when the
guard refuses or is not found. Read and comment go through the omnibase_infra
Linear project-tracker adapter. ``LINEAR_API_KEY`` must resolve; there is no
stub. Any refusal or failure prints ``status="error"`` with the reason in
``validation_errors`` and exits 1.

Usage:
    python -m omnimarket.nodes.node_create_ticket --operation read --ticket-id OMN-1800
    python -m omnimarket.nodes.node_create_ticket --operation comment --ticket-id OMN-1800 --body "text"
    python -m omnimarket.nodes.node_create_ticket --operation transition --ticket-id OMN-1800 --state Canceled
    python -m omnimarket.nodes.node_create_ticket --title "Add rate limiting" --parent OMN-1800 --description-file body.md
    python -m omnimarket.nodes.node_create_ticket --title "Add rate limiting" --parent OMN-1800 --description-file body.md --dry-run

Outputs JSON to stdout: ModelCreateTicketResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    EnumTicketOperation,
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Create, read or comment on a Linear ticket."
    )
    parser.add_argument(
        "--operation",
        default=EnumTicketOperation.CREATE.value,
        choices=[op.value for op in EnumTicketOperation],
        help="create (default), read, comment or transition.",
    )
    parser.add_argument("--title", default="", help="Ticket title (create).")
    parser.add_argument(
        "--description", default="", help="Ticket description body (create)."
    )
    parser.add_argument(
        "--description-file",
        default="",
        help="Read the description (create) from this file, byte for byte.",
    )
    parser.add_argument(
        "--ticket-id", default="", help="Ticket id, e.g. OMN-1234 (read, comment)."
    )
    parser.add_argument("--body", default="", help="Comment body (comment).")
    parser.add_argument(
        "--body-file", default="", help="Read the comment body from this file."
    )
    parser.add_argument(
        "--state", default="", help="Target workflow state, e.g. Canceled (transition)."
    )
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
        "--pillar",
        default="",
        help="Ticket pillar (dashboard, onboarding); sets the assignee from the shared owner map.",
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
        help="Validate and run the guard without issuing any Linear write.",
    )

    args = parser.parse_args()
    description = (
        Path(args.description_file).read_text(encoding="utf-8")
        if args.description_file
        else args.description
    )
    body = (
        Path(args.body_file).read_text(encoding="utf-8")
        if args.body_file
        else args.body
    )

    blocked_by: list[str] = (
        [b.strip() for b in args.blocked_by.split(",") if b.strip()]
        if args.blocked_by
        else []
    )

    operation = EnumTicketOperation(args.operation)
    try:
        request = ModelCreateTicketRequest(
            operation=operation,
            title=args.title,
            description=description,
            ticket_id=args.ticket_id or None,
            body=body,
            state=args.state,
            repo=args.repo or None,
            parent=args.parent or None,
            blocked_by=blocked_by,
            team=args.team,
            pillar=args.pillar or None,
            dry_run=args.dry_run,
        )
        result = HandlerCreateTicket().handle(request)
    except (RuntimeError, ValueError, OSError) as exc:
        # A guard refusal, a missing guard or key, an invalid request, or the
        # fail-closed empty-id check (OMN-14547) -- reported as a structured
        # error, preserving this CLI's "always prints a ModelCreateTicketResult
        # JSON" contract. The exception text never carries the key.
        result = ModelCreateTicketResult(
            status="error",
            operation=operation,
            title=args.title,
            ticket_id=args.ticket_id,
            team=args.team,
            validation_errors=[f"{type(exc).__name__}: {exc}"],
            dry_run=args.dry_run,
        )

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
