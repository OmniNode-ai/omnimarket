# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The write record: one event per successful write of a ticket by the ticket node (OMN-23760, OMN-23761)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketContractWritten(BaseModel):
    """Consumed from onex.evt.omnimarket.ticket-contract-written.v1.

    content_sha256 is the sha256 of the title and description as Linear returns them after the
    write (title, a blank line, description), so Linear's own markdown normalization is never
    mistaken for a hand edit. linear_updated_at is the issue's updatedAt read at that moment.
    A foreign producer may grow the event; this consumer ignores fields it does not read.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ticket_id: str = Field(min_length=1)
    contract_fingerprint: str = ""
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    linear_updated_at: datetime
    correlation_id: UUID | None = None
