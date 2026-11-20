# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of node_ticket_query's list_unowned_sprint_tickets operation.

The fill places lanes from the open tickets of the current sprint that no lane holds. The sprint is
resolved by date from Linear at the time of the call, and a ticket is held when the rolling ledger
has an open CLAIM naming it (or its lane) with no later TERMINAL.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    ModelSprintResolution,
)


class ModelUnownedSprintTicketsInput(BaseModel):
    """The one bound of the read: how far back the ledger is read for CLAIM and TERMINAL rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_window_hours: int = Field(
        default=168,
        ge=1,
        le=720,
        description="Ledger rows older than this are not read, so a CLAIM older than it is not live.",
    )


class ModelUnownedSprintTicket(BaseModel):
    """One open ticket of the current sprint with no live CLAIM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    title: str = ""
    state_name: str = ""
    state_type: str = ""
    assignee_id: str = ""
    labels: tuple[str, ...] = ()


class ModelUnownedSprintTicketsOutput(BaseModel):
    """The sprint read and the open tickets of it with no live CLAIM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sprint: ModelSprintResolution
    tickets: tuple[ModelUnownedSprintTicket, ...] = ()
    open_in_sprint: int = Field(
        default=0, description="Open tickets of the sprint before the CLAIM filter."
    )
    claimed: tuple[str, ...] = Field(
        default=(),
        description="Open sprint tickets left out because a live CLAIM holds them.",
    )
    ledger_status: str = Field(
        default="",
        description="The ledger reader's status for the CLAIM and TERMINAL read.",
    )


__all__ = [
    "ModelUnownedSprintTicket",
    "ModelUnownedSprintTicketsInput",
    "ModelUnownedSprintTicketsOutput",
]
