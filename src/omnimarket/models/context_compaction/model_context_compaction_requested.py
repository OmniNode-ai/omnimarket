# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction command the adapter publishes (OMN-21113, TA.11).

It carries no conversation content: every source the package needs is read
behind the bus. ``invocation_id`` is minted by the adapter for every invocation
and is echoed on the package reply and on any ``-fallback.v1`` event.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_compaction.enum_context_compaction_trigger import (
    EnumContextCompactionTrigger,
)


class ModelContextCompactionRequested(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    session_id: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    trigger: EnumContextCompactionTrigger
    input_messages_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    newest_turn: str | None = None
    compact_model: str | None = Field(default=None, min_length=1)


__all__ = ["ModelContextCompactionRequested"]
