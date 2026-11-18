# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus models of node_floor_task_ticket_effect: the mint command, its completed answer, and the overlay's bus section."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_task_ticket_effect.models.model_floor_task_ticket_effect import (
    ModelFloorTaskTicketMintResult,
)


class ModelFloorTaskTicketMintCommand(BaseModel):
    """One bus mint command: the payload of one floor task's ticket and its caller identity.

    It carries no timeout: the deadline of each call of the ticket store is the overlay's
    ``floor_fill.mint_timeout_seconds``, which the handler reads when it handles the command.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by_lane: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    floor_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parent: str = Field(pattern=r"^OMN-[0-9]+$")


class ModelFloorTaskTicketMintCompleted(BaseModel):
    """The completed-topic answer of a bus mint command: the same result, correlated by the command's id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by_lane: str
    floor_key: str
    outcome: Literal["completed"] = "completed"
    result: ModelFloorTaskTicketMintResult


class ModelFloorTaskTicketBusOverlay(BaseModel):
    """The bus section of the node overlay: the deadline of each store call and the store the bus binds.

    No defaults: a missing key refuses naming it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mint_timeout_seconds: float = Field(gt=0)
    ticket_store: Literal["inmemory", "linear"]


class ModelFloorTaskTicketOverlayDocument(BaseModel):
    """The node's overlay document: ``floor_fill`` with the bus section's keys.

    The document also carries ``event_bus`` (the bus ``onex run-node`` dials, read by the broker resolver), which this model
    ignores; inside ``floor_fill`` an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    floor_fill: ModelFloorTaskTicketBusOverlay
