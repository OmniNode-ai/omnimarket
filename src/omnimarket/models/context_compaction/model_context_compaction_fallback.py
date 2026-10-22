# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-fallback.v1`` receipt: ids and a failure class, never content (OMN-21113, TA.11)."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_compaction.enum_context_compaction_failure_class import (
    EnumContextCompactionFailureClass,
)
from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
)


class ModelContextCompactionFallback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    IDENTITY_FIELD: ClassVar[str] = "invocation_id"

    invocation_id: UUID
    session_id: SessionId
    candidate_id: UUID | None = None
    failure_class: EnumContextCompactionFailureClass
    #: Wall time of the pack build in milliseconds; None when the barrier failed first (TA.12).
    build_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


__all__ = ["ModelContextCompactionFallback"]
