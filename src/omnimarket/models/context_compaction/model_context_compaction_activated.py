# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-activated.v1`` receipt: this invocation offered this candidate (OMN-21113, TA.11)."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction.enum_context_compaction_trigger import (
    EnumContextCompactionTrigger,
)
from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    Sha256Hex,
)


class ModelContextCompactionActivated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    IDENTITY_FIELD: ClassVar[str] = "activation_id"

    activation_id: UUID
    candidate_id: UUID
    invocation_id: UUID
    session_id: SessionId
    trigger: EnumContextCompactionTrigger
    model_outputs_hash: Sha256Hex
    final_pack_hash: Sha256Hex
    body_hash: Sha256Hex
    installed_messages_hash: Sha256Hex


__all__ = ["ModelContextCompactionActivated"]
