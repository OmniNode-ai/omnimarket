# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one handled input did; the handler's return."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.enum_ticket_hand_edit_outcome import (
    EnumTicketHandEditOutcome,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.model_ticket_hand_edit_finding import (
    ModelTicketHandEditFinding,
)


class ModelTicketHandEditResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumTicketHandEditOutcome
    reason: str | None = None
    findings: tuple[ModelTicketHandEditFinding, ...] = ()
    checked: int = Field(default=0, ge=0)
    closed: int = Field(default=0, ge=0)
    error: str | None = None
