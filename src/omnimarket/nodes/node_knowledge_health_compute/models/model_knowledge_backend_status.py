# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Classified status for a single knowledge backend."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState


class ModelKnowledgeBackendStatus(BaseModel):
    """Classified health status for one knowledge backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    freshness_state: EnumKnowledgeFreshnessState
    entry_count: int
    last_updated_seconds_ago: int | None
    drift_detected: bool
    error: str | None


__all__ = ["ModelKnowledgeBackendStatus"]
