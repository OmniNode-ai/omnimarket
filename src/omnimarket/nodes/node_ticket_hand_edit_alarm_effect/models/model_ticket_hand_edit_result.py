# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one handled message did; logged by the entries, never published."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.enum_ticket_hand_edit import (
    EnumTicketHandEditOutcome,
)


class ModelTicketHandEditResult(BaseModel):
    """Counts of one sweep or one recorded write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumTicketHandEditOutcome
    reason: str | None = None
    issues_read: int = 0
    findings_raised: tuple[str, ...] = ()
    findings_already_open: tuple[str, ...] = ()
    findings_closed: tuple[str, ...] = ()
    cursor_before: datetime | None = None
    cursor_after: datetime | None = None
    error: str | None = None
