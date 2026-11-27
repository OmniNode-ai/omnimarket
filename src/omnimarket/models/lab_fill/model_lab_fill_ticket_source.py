# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``ticket_source`` key of the selection overlay: which adapter serves open launch work (OMN-26762)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillTicketSource(BaseModel):
    """The adapter name and the filters the fill passes to ``ProtocolTicketService.list_tickets``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str = Field(min_length=1)
    assignee: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    status_types: tuple[str, ...] = ()
    limit: int = Field(default=250, ge=1)


__all__ = ["ModelLabFillTicketSource"]
