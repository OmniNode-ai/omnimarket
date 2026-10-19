# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-install-uncertain.v1`` receipt: an activation with no installation fact and a typed reason (OMN-21112, TA.10)."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction.enum_context_install_uncertain_reason import (
    EnumContextInstallUncertainReason,
)
from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    TranscriptRecordId,
)


class ModelContextCompactionInstallUncertain(BaseModel):
    """``boundary_uuid`` names the boundary the reason was decided at; a capture gap has none."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    IDENTITY_FIELD: ClassVar[str] = "activation_id"

    activation_id: UUID
    session_id: SessionId
    reason: EnumContextInstallUncertainReason
    boundary_uuid: TranscriptRecordId | None = None


__all__ = ["ModelContextCompactionInstallUncertain"]
