# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed error event, and the slack-publish command raised under the same key."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.enum_ticket_hand_edit import (
    EnumTicketHandEditFindingKind,
)


class ModelTicketHandEditFinding(BaseModel):
    """One ticket_hand_edit or detector_unreadable finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumTicketHandEditFindingKind
    idempotency_key: str = Field(..., min_length=1)
    ticket_id: str | None = None
    fingerprint_written: str | None = Field(
        default=None, description="The fingerprint the node's newest write recorded."
    )
    fingerprint_found: str | None = Field(
        default=None,
        description="The fingerprint recomputed from the block found in Linear; None when no block parsed.",
    )
    content_sha256_written: str | None = None
    content_sha256_found: str | None = None
    differs: tuple[str, ...] = Field(default=(), description="What differs, in words.")
    editors: tuple[str, ...] = Field(
        default=(),
        description="Actor ids of the description revisions after the node's last write.",
    )
    reason: str | None = Field(
        default=None, description="detector_unreadable: the source and the error."
    )
    detected_at: datetime
    correlation_id: UUID

    def text(self) -> str:
        """The alarm's one-paragraph Slack text."""
        if self.kind is EnumTicketHandEditFindingKind.DETECTOR_UNREADABLE:
            return f"ticket hand-edit detector_unreadable: {self.reason}"
        editors = ", ".join(self.editors) or "unknown"
        return (
            f"ticket_hand_edit {self.ticket_id}: edited outside the ticket node by {editors}; "
            f"fingerprint written {self.fingerprint_written or 'none'}, "
            f"found {self.fingerprint_found or 'none'}; " + "; ".join(self.differs)
        )


class ModelTicketHandEditSlackCommand(BaseModel):
    """Field-for-field wire mirror of ModelSlackPublish; extra fields fail closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(..., min_length=1)
    blocks: list[dict[str, Any]] | None = None
    text: str | None = None
    thread_ts: str | None = None
    idempotency_key: str = Field(..., min_length=1)
    correlation_id: UUID
