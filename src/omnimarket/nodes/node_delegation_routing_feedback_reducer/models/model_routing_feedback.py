# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelRoutingFeedback — accumulated per-(model_id, task_type) routing signal."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRoutingFeedback(BaseModel):
    """Accumulated routing feedback for one (model_id, task_type) pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    task_type: str
    success_count: int = 0
    failure_count: int = 0
    escalation_count: int = 0
    total_count: int = 0
    success_rate: float = 0.0
    escalation_rate: float = 0.0
    avg_latency_ms: float = 0.0
    window_start: str = ""
    last_updated: str = ""


class ModelRoutingFeedbackUpdatedEvent(BaseModel):
    """Event emitted to routing-feedback-updated.v1 after accumulating a terminal event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    feedback: ModelRoutingFeedback
    source_topic: str


__all__ = ["ModelRoutingFeedback", "ModelRoutingFeedbackUpdatedEvent"]
