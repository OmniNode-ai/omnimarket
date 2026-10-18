# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-fallback.v1`` receipt: ids and a failure class, never content (OMN-21113, TA.11)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction.enum_context_compaction_failure_class import (
    EnumContextCompactionFailureClass,
)


class ModelContextCompactionFallback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    session_id: str
    candidate_id: UUID | None = None
    failure_class: EnumContextCompactionFailureClass


__all__ = ["ModelContextCompactionFallback"]
