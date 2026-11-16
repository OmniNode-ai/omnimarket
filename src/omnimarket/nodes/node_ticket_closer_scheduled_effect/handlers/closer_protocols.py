# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three seams the scheduled ticket closer drives (OMN-24301)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.ticket_closer import (
    ModelCloserCandidate,
    ModelCloserVerifyReading,
    ModelCloserWriteReceipt,
)


@runtime_checkable
class ProtocolCloserCandidateSource(Protocol):
    """One read of the closer's candidate tickets for one run.

    ``None`` is the source that could not read: the reading is unread, never an empty success.
    """

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None: ...


@runtime_checkable
class ProtocolCloserVerifier(Protocol):
    """One run of the DoD verifier for one ticket against one execution audience."""

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading: ...


@runtime_checkable
class ProtocolTicketDoneWriter(Protocol):
    """One move of a ticket to the done state through the ticket write node."""

    def mark_done(
        self, ticket_id: str, *, state: str, lane: str, evidence: str
    ) -> ModelCloserWriteReceipt: ...
