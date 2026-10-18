# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-activated.v1`` receipt: this invocation offered this candidate (OMN-21113, TA.11)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction.enum_context_compaction_trigger import (
    EnumContextCompactionTrigger,
)


class ModelContextCompactionActivated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    activation_id: UUID
    candidate_id: UUID
    invocation_id: UUID
    session_id: str
    trigger: EnumContextCompactionTrigger
    model_outputs_hash: str
    final_pack_hash: str
    body_hash: str
    installed_messages_hash: str


__all__ = ["ModelContextCompactionActivated"]
