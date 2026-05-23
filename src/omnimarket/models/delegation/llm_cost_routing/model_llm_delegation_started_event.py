# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for a delegation attempt being started against a specific model."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelLlmDelegationStartedEvent(BaseModel):
    """Event emitted when a delegation attempt is started against a specific model.

    Corresponds to ModelDelegationAttempted in the design doc. Records the
    endpoint reference (env var name, not raw URL) for contract-driven resolution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None
    model_id: str
    model_tier: str
    endpoint_ref: str  # env var name declared in contract, not a raw URL
    attempt_number: int
    created_at: datetime
