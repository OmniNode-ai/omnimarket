# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input union model for delegation terminal events consumed by the feedback reducer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDelegationFeedbackEventType(StrEnum):
    COMPLETED = "delegation-call-completed"
    ESCALATION_TRIGGERED = "delegation-escalation-triggered"
    ALL_TIERS_FAILED = "delegation-all-tiers-failed"


class ModelDelegationFeedbackEvent(BaseModel):
    """Normalized input for the feedback reducer.

    Derived from the three terminal event schemas — only the fields required
    for feedback accumulation are retained here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: EnumDelegationFeedbackEventType
    correlation_id: str
    request_id: str
    task_type: str
    model_id: str
    # True only for delegation-call-completed where success=True
    success: bool
    # True when this event is an escalation trigger
    is_escalation: bool
    # Latency present only on completed events; 0 otherwise
    latency_ms: int = 0
    source_topic: str = ""


__all__ = ["EnumDelegationFeedbackEventType", "ModelDelegationFeedbackEvent"]
