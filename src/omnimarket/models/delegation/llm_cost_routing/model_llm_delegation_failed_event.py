# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for a failed LLM delegation attempt."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


class ModelLlmDelegationFailedEvent(BaseModel):
    """Event emitted when a single LLM delegation attempt fails.

    Distinct from ModelLlmDelegationAllTiersFailedEvent: this covers a single
    attempt failure that may be retried with a different model tier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None
    model_id: str
    model_tier: str
    attempt_number: int
    failure_class: EnumDelegationFailureClass
    failure_reason: str
    created_at: datetime
