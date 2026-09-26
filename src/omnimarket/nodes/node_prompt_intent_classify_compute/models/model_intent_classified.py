# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outbound event on onex.evt.omniintelligence.intent-classified.v1.

The field names are the ones the intent-classification projection reads
(``projection_intent_classification``, UPSERT by ``correlation_id``), so the
event needs no alias on the consuming side. ``event_type`` keeps the value the
retired omniintelligence publisher used, for any reader keyed on it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelIntentClassified(BaseModel):
    """One classified prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["IntentClassified"] = "IntentClassified"
    session_id: str
    correlation_id: str
    intent_class: str
    intent_category: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list)
    emitted_at: str | None = None
    agent_source: str = "claude"


__all__ = ["ModelIntentClassified"]
