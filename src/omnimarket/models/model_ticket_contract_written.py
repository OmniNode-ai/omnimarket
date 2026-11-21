# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket node's write record: one event per write of a standard ticket (OMN-23776).

Published on ``onex.evt.omnimarket.ticket-contract-written.v1`` by the ticket node after
every write it makes to a ticket (create, sync) and after every pull that adopts the
ticket's current text. The hand-edit detector compares Linear against the newest one per
ticket. The content hash covers the title and description exactly as Linear returned them
after the write, so Linear's own markdown normalization is never read as a hand edit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def ticket_content_sha256(title: str, description: str) -> str:
    """The sha256 of a ticket's title and description; the one definition both sides use."""
    payload = json.dumps(
        [title, description], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModelTicketContractWritten(BaseModel):
    """One node write (or adoption) of one ticket; producers may add fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ticket_id: str = Field(..., min_length=1, description="The identifier, e.g. OMN-1.")
    contract_fingerprint: str = Field(
        default="", description="The fingerprint the node wrote; empty when none."
    )
    content_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="ticket_content_sha256 of the title and description after the write.",
    )
    updated_at: datetime = Field(
        ..., description="The issue's updatedAt as Linear returned it after the write."
    )
    kind: Literal["written", "adopted"] = "written"
    correlation_id: UUID | None = None


__all__ = ["ModelTicketContractWritten", "ticket_content_sha256"]
