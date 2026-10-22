# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-prepared.v1`` receipt: one deterministic assembly, no model-dependent hash (OMN-21113, TA.11)."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_compaction.model_context_compaction_fields import (
    Name,
    SessionId,
    Sha256Hex,
)


class ModelContextCompactionPrepared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    IDENTITY_FIELD: ClassVar[str] = "candidate_id"

    candidate_id: UUID
    session_id: SessionId
    input_messages_hash: Sha256Hex
    assembly_hash: Sha256Hex
    version_vector_hash: Sha256Hex
    profile_name: Name
    profile_content_hash: Sha256Hex
    #: Wall time of the pack build in milliseconds (TA.12).
    build_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    #: The model-written section's share of the build in milliseconds (TA.12).
    model_section_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


__all__ = ["ModelContextCompactionPrepared"]
