# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared intelligence event envelopes for omnimarket nodes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnibase_core.enums.intelligence.enum_intent_class import EnumIntentClass
from pydantic import BaseModel, ConfigDict, Field


class ModelIntentClassifiedEnvelope(BaseModel):
    """Frozen event envelope for intent classification events."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    event_type: str = Field(default="IntentClassified")
    session_id: str
    correlation_id: UUID
    intent_class: EnumIntentClass
    confidence: float = Field(..., ge=0.0, le=1.0)
    fallback: bool = False
    emitted_at: datetime


class ModelIntentDriftDetectedEnvelope(BaseModel):
    """Frozen event envelope for intent drift events."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    event_type: str = Field(default="IntentDriftDetected")
    session_id: str
    correlation_id: str
    declared_intent: EnumIntentClass
    observed_intent: EnumIntentClass
    drift_score: float = Field(..., ge=0.0, le=1.0)
    emitted_at: datetime


class ModelIntentOutcomeLabeledEnvelope(BaseModel):
    """Frozen event envelope for labeled intent outcome events."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    event_type: str = Field(default="IntentOutcomeLabeled")
    session_id: str
    correlation_id: str
    intent_class: EnumIntentClass
    success: bool
    cost_usd: float = Field(default=0.0, ge=0.0)
    emitted_at: datetime


class ModelIntentPatternPromotedEnvelope(BaseModel):
    """Frozen event envelope for intent pattern promotion events."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    event_type: str = Field(default="IntentPatternPromoted")
    pattern_id: UUID
    correlation_id: str
    intent_class: EnumIntentClass
    pattern_signature: str = Field(..., min_length=1, max_length=500)
    promotion_confidence: float = Field(..., ge=0.0, le=1.0)
    emitted_at: datetime


__all__ = [
    "ModelIntentClassifiedEnvelope",
    "ModelIntentDriftDetectedEnvelope",
    "ModelIntentOutcomeLabeledEnvelope",
    "ModelIntentPatternPromotedEnvelope",
]
