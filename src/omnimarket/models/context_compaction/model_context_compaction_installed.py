# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-installed.v1`` receipt: a captured boundary carries the activation's messages (OMN-21112, TA.10)."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    Sha256Hex,
    TranscriptRecordId,
)


class ModelContextCompactionInstalled(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    IDENTITY_FIELD: ClassVar[str] = "activation_id"

    activation_id: UUID
    session_id: SessionId
    boundary_uuid: TranscriptRecordId
    observed_hash: Sha256Hex


__all__ = ["ModelContextCompactionInstalled"]
