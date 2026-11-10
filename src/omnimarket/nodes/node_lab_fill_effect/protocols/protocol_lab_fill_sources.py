# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the lab-fill enumerate operation (OMN-23250)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .protocol_lab_fill_effect import LabFillPortError


class LabFillSourceCutError(LabFillPortError):
    """A read ran out of the budget it was given. The handler marks that one source stale."""


class LabFillRateLimitedError(LabFillPortError):
    """Linear refused a read by rate (HTTP 429). The ticket stage then falls back to the mirror's older copies."""


@dataclass(frozen=True)
class LabFillMirroredTicket:
    """One ticket as the read-only Linear mirror holds it; ``has_children`` is None when the copy never read children."""

    ticket: str
    title: str
    state_type: str
    has_children: bool | None
    read_at_epoch_s: float


@dataclass(frozen=True)
class LabFillTicketNode:
    """A listed ticket as source-tickets read it."""

    ticket: str
    title: str
    state_type: str
    has_children: bool
    open_prs: tuple[str, ...]


@dataclass(frozen=True)
class LabFillListed:
    """One source's list: (ticket, evidence) pairs and the tickets it dropped itself, by reason."""

    found: tuple[tuple[str, str], ...]
    dropped: tuple[tuple[str, int], ...] = ()


class ProtocolLabFillTicketMirror(Protocol):
    """The read-only Linear mirror: each wanted ticket's copy, whatever its age; a ticket it does not hold is absent."""

    def __call__(
        self, tickets: Sequence[str]
    ) -> Mapping[str, LabFillMirroredTicket]: ...


class ProtocolLabFillPlansCopy(Protocol):
    """The saved plans-today read and the one whole-ledger read that rebuilds it."""

    def age_min(self) -> float | None:
        """Minutes since the copy was written, or None when there is none."""
        ...

    def load(self) -> LabFillListed:
        """The saved copy's list."""
        ...

    def rebuild(self, budget_s: float) -> LabFillListed:
        """Read the plans (the whole-ledger read) inside ``budget_s``, save the copy; LabFillSourceCutError when cut."""
        ...


class ProtocolLabFillSourceReaders(Protocol):
    """The three list sources and the ticket stage. Each takes the seconds it may use and raises
    ``LabFillSourceCutError`` when they run out, ``LabFillPortError`` when it cannot read."""

    def read_rulings(self, budget_s: float) -> LabFillListed: ...

    def read_resends(self, budget_s: float) -> LabFillListed: ...

    def read_tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillTicketNode]: ...
