# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for a model being temporarily degraded for a task type."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelLlmDelegationModelDegradedEvent(BaseModel):
    """Event emitted when a model exceeds escalation rate threshold for a task type.

    Published to onex.evt.omnimarket.delegation-model-degraded.v1.

    Degradation is always time-bounded via expires_at. It does not become an
    unbounded global ban. After expiry the model is re-eligible and its next
    window starts fresh. Reducers must not accumulate degradation state indefinitely.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    task_type: str
    model_id: str
    window_start: datetime
    window_end: datetime
    attempt_count: int
    escalation_count: int
    threshold: float
    expires_at: datetime  # degradation is time-bounded, not an unbounded global ban
    reason: str
    created_at: datetime
