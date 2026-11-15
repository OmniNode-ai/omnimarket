# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory ticket store: a dict of tickets that answers the four operations of ProtocolClusterTicketStore.

Its ``create`` numbers the tickets in order and stores the description as given; ``search``
returns every ticket whose description contains any needle (the tracker's ``description
contains`` read); ``comment`` appends to a per-ticket list readable as ``comments[ticket]``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import ClassVar

from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCommentResult,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
    ModelClusterTicketSearchResult,
)


@dataclass
class StoredClusterTicket:
    """One ticket of the in-memory store."""

    description: str
    status: str = "open"
    duplicate_of: str | None = None


@dataclass
class InMemoryClusterTicketStore:
    """A ticket store held in a dict; the first ticket it creates is ``<prefix>-<first_number>``."""

    synchronous_execution: ClassVar[bool] = True

    prefix: str = "OMN"
    first_number: int = 90001
    tickets: dict[str, StoredClusterTicket] = field(default_factory=dict)
    comments: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def seed(
        self,
        ticket: str,
        description: str,
        status: str = "open",
        duplicate_of: str | None = None,
    ) -> None:
        """Put an existing ticket in the store, as the tracker already holds it."""
        self.tickets[ticket] = StoredClusterTicket(
            description=description, status=status, duplicate_of=duplicate_of
        )

    def set_status(self, ticket: str, status: str) -> None:
        self.tickets[ticket].status = status

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        stored = self.tickets.get(request.ticket)
        if stored is None:
            return ModelClusterTicketLookupResult(state="missing")
        return ModelClusterTicketLookupResult(
            state="found",
            ticket=request.ticket,
            status=stored.status,
            description=stored.description,
            duplicate_of=stored.duplicate_of,
        )

    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(
            state="answered",
            tickets=tuple(
                ticket
                for ticket, stored in self.tickets.items()
                if any(needle in stored.description for needle in request.needles)
            ),
        )

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        number = self.first_number + len(self.tickets)
        while f"{self.prefix}-{number}" in self.tickets:
            number += 1
        ticket = f"{self.prefix}-{number}"
        self.tickets[ticket] = StoredClusterTicket(description=request.description)
        return ModelClusterTicketCreateResult(state="created", ticket=ticket)

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        if request.ticket not in self.tickets:
            return ModelClusterTicketCommentResult(
                state="failed", cause="ticket-missing"
            )
        self.comments[request.ticket].append(request.body)
        return ModelClusterTicketCommentResult(
            state="commented",
            comment_id=f"{request.ticket}-comment-{len(self.comments[request.ticket])}",
        )
