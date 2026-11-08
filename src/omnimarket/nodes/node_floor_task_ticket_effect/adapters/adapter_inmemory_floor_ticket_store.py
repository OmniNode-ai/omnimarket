# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory ticket store: a dict of tickets that answers the three operations of ProtocolFloorTicketStore.

Its ``create`` runs the gate it is given on the payload (the profile's stand-in for ``node_create_ticket``'s gate) and
numbers the tickets in order; ``search_by_floor_key`` matches the exact ``Floor key:`` line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
    ModelFloorTicketLookupRequest,
    ModelFloorTicketLookupResult,
    ModelFloorTicketSearchRequest,
    ModelFloorTicketSearchResult,
)

from omnimarket.nodes.node_floor_task_ticket_effect.handlers.ticket_judge import (
    carries_floor_key_line,
)

# A gate takes the payload and returns the refusal cause, or None to admit.
Gate = Callable[[ModelFloorTicketCreateRequest], str | None]


def _admit_all(_request: ModelFloorTicketCreateRequest) -> str | None:
    return None


@dataclass
class StoredTicket:
    """One ticket of the in-memory store."""

    description: str
    status: str = "Backlog"
    project: str | None = None
    labels: tuple[str, ...] = ()
    pillar: str | None = None
    has_children: bool = False


@dataclass
class InMemoryFloorTicketStore:
    """A ticket store held in a dict; the first ticket it creates is ``<prefix>-<first_number>``."""

    synchronous_execution: ClassVar[bool] = True

    gate: Gate = field(default=_admit_all)
    prefix: str = "OMN"
    first_number: int = 1
    tickets: dict[str, StoredTicket] = field(default_factory=dict)

    def read(
        self, request: ModelFloorTicketLookupRequest
    ) -> ModelFloorTicketLookupResult:
        stored = self.tickets.get(request.ticket)
        if stored is None:
            return ModelFloorTicketLookupResult(state="missing")
        return ModelFloorTicketLookupResult(
            state="found",
            ticket=request.ticket,
            status=stored.status,
            description=stored.description,
            project=stored.project,
            labels=stored.labels,
            pillar=stored.pillar,
            has_children=stored.has_children,
        )

    def search_by_floor_key(
        self, request: ModelFloorTicketSearchRequest
    ) -> ModelFloorTicketSearchResult:
        return ModelFloorTicketSearchResult(
            state="answered",
            tickets=tuple(
                ticket
                for ticket, stored in self.tickets.items()
                if carries_floor_key_line(stored.description, request.floor_key)
            ),
        )

    def create(
        self, request: ModelFloorTicketCreateRequest
    ) -> ModelFloorTicketCreateResult:
        refusal = self.gate(request)
        if refusal is not None:
            return ModelFloorTicketCreateResult(state="refused", cause=refusal)
        ticket = f"{self.prefix}-{self.first_number + len(self.tickets)}"
        self.tickets[ticket] = StoredTicket(description=request.description)
        return ModelFloorTicketCreateResult(state="created", ticket=ticket)
