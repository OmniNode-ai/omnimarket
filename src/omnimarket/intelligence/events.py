# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared intelligence event envelopes for omnimarket nodes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnibase_core.enums.enum_correction_failure_axis import EnumCorrectionFailureAxis
from omnibase_core.enums.enum_user_correction_category import EnumUserCorrectionCategory
from omnibase_core.enums.intelligence.enum_intent_class import EnumIntentClass
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class ModelUserCorrectionEvent(BaseModel):
    """Typed, category-weighted user-correction event (OMN-12846).

    A user correction of the agent's work, categorized by *what kind* of
    correction it is (``category``) and dimensioned by whether it counts against
    context selection (``failure_axis``). The two axes are first-class and never
    collapsed into a single rolled-up score.

    Every event is linked to the context pack / factor subset that was in play
    via mandatory ``context_pack_hash`` and ``factor_subset_hash`` (same sha256
    convention as ``model_generation``). A correction with no resolvable context
    link is an orphan signal and is rejected at the validator, not silently
    accepted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    event_type: str = Field(default="UserCorrection")
    session_id: str
    correlation_id: UUID
    category: EnumUserCorrectionCategory
    failure_axis: EnumCorrectionFailureAxis
    context_pack_hash: str = Field(
        ...,
        min_length=1,
        description=(
            "SHA-256 digest (sha256:<hex>) of the context pack in play. "
            "Mandatory: links the correction to the injected context."
        ),
    )
    factor_subset_hash: str = Field(
        ...,
        min_length=1,
        description=(
            "SHA-256 digest (sha256:<hex>) of the factor subset in play. "
            "Mandatory: links the correction to the selected factors."
        ),
    )
    emitted_at: datetime

    @field_validator("context_pack_hash", "factor_subset_hash")
    @classmethod
    def _reject_blank_hash(cls, value: str) -> str:
        """Reject whitespace-only hashes so the signal is never orphaned."""
        if not value.strip():
            raise ValueError(
                "context_pack_hash and factor_subset_hash must be non-empty; "
                "a correction with no resolvable context link is an orphan signal"
            )
        return value

    @property
    def counts_toward_context_failure(self) -> bool:
        """Whether this correction counts against context selection.

        Only MISUNDERSTANDING-axis corrections are context-selection failures.
        NEW_INFORMATION is recorded but excluded from the context-failure rate.
        """
        return self.failure_axis is EnumCorrectionFailureAxis.MISUNDERSTANDING


__all__ = [
    "ModelIntentClassifiedEnvelope",
    "ModelIntentDriftDetectedEnvelope",
    "ModelIntentOutcomeLabeledEnvelope",
    "ModelIntentPatternPromotedEnvelope",
    "ModelUserCorrectionEvent",
]
