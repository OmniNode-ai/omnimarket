# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Probe result for a single knowledge backend."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState


class ModelKnowledgeBackendProbe(BaseModel):
    """Raw probe data collected from one knowledge backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    freshness_state: EnumKnowledgeFreshnessState = EnumKnowledgeFreshnessState.UNKNOWN
    entry_count: int = 0
    last_updated_seconds_ago: int | None = None
    drift_detected: bool = False
    error: str | None = None


__all__ = ["ModelKnowledgeBackendProbe"]
