# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for an LLM delegation escalation being triggered."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


class ModelLlmDelegationEscalationTriggeredEvent(BaseModel):
    """Event emitted when output fails quality gate and escalation is triggered.

    Published to the delegation-escalation-triggered topic declared in contract.yaml.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None
    model_id: str
    attempt_number: int
    failure_class: EnumDelegationFailureClass
    escalation_reason: str
    next_model_id: str | None
    created_at: datetime
