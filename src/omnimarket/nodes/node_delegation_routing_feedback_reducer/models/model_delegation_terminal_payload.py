# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed terminal payload accepted by the runtime-coerced feedback handler."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_feedback_event import (
    EnumDelegationFeedbackEventType,
)


class ModelDelegationTerminalPayload(BaseModel):
    """Terminal identity and structural signals; ignore unrelated wire fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = ""
    request_id: str = ""
    task_type: str = ""
    model_id: str = ""
    success: bool = False
    latency_ms: object = 0
    attempted_models: tuple[str, ...] = ()
    escalation_reason: str | None = None
    next_model_id: str | None = None
    attempt_number: int | None = None
    event_type: EnumDelegationFeedbackEventType | None = None


__all__ = ["ModelDelegationTerminalPayload"]
