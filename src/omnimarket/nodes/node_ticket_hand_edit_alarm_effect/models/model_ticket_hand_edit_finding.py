# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed error event of a finding (ticket_hand_edit or detector_unreadable)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.enum_ticket_hand_edit_kind import (
    EnumTicketHandEditKind,
)


class ModelTicketHandEditFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumTicketHandEditKind
    ticket_id: str = Field(min_length=1)
    written_fingerprint: str = ""
    found_fingerprint: str = ""
    written_content_sha256: str = ""
    found_content_sha256: str = ""
    differences: tuple[str, ...] = ()
    editors: tuple[str, ...] = ()
    reason: str | None = None
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    correlation_id: UUID
