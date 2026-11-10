# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation terminal a typed decision leaves behind (OMN-23334).

A typed-decision result used to leave only a log line, so the delegation census
(which counts ``delegation_events`` rows per provider) could never count a Jev
decision. This module turns one ``ModelTypedDecisionResult`` into the same
``delegate-skill`` terminal the projection already folds into ``delegation_events``
for harness runs, and hands it to an injected publisher.

Everything about the row's shape that is data (the two terminal topics, the wire
event type, the task type, the provider label and the tenant) comes from the
node contract's ``delegation_event`` block, never from this module.

The prompt and the work-unit state are never put in the event: the decision
backend is a third party and the row records the outcome, not the question.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    ModelTypedDecisionRequest,
    ModelTypedDecisionResult,
)

_LOGGER = logging.getLogger(__name__)
# The wire's closed acceptance reasons: the model decided, the backend answered
# below the abstention threshold, or the call produced no usable answer.
_REASON_QUALITY_MET = "quality_bar_met"
_REASON_BELOW_BAR = "score_below_required_bar"
_REASON_CALL_FAILED = "provider_call_failed"


class ModelTypedDecisionEventRouting(BaseModel):
    """The contract's ``delegation_event`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    completed_topic: str = Field(..., min_length=1)
    failed_topic: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)


class ModelTypedDecisionEvent(BaseModel):
    """One terminal, ready for a publisher."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    event_type: str
    event_id: str
    correlation_id: str
    payload: dict[str, object]


class ProtocolTypedDecisionEventPublisher(Protocol):
    """Deliver one event to one declared topic. Never raises into the decision."""

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool: ...


class InMemoryTypedDecisionEventPublisher:
    """The default profile: keeps what it was given, sends nothing."""

    def __init__(self) -> None:
        self.events: list[ModelTypedDecisionEvent] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        self.events.append(
            ModelTypedDecisionEvent(
                topic=topic,
                event_type=event_type,
                event_id=event_id,
                correlation_id=correlation_id or "",
                payload=dict(payload),
            )
        )
        return True


def _backend_answered(result: ModelTypedDecisionResult) -> bool:
    """True when the backend returned a schema-valid answer (abstention included)."""
    return result.http_status == 200 and result.probability is not None


def build_typed_decision_event(
    request: ModelTypedDecisionRequest,
    result: ModelTypedDecisionResult,
    routing: ModelTypedDecisionEventRouting,
    *,
    emitted_at: datetime,
) -> ModelTypedDecisionEvent:
    """Map one decision result to its delegate-skill terminal."""
    answered = _backend_answered(result)
    model_decided = result.decided_by is EnumTypedDecisionDecider.MODEL
    detail = (
        f"{result.reason.value}: {result.detail}"
        if result.reason is not None and result.detail
        else (result.reason.value if result.reason is not None else "")
    )
    answer = result.answer or ""
    served = result.served_model or result.requested_model or ""
    input_tokens = result.input_tokens or 0
    output_tokens = result.output_tokens or 0
    correlation_id = str(result.correlation_id)
    payload: dict[str, object] = {
        "status": "completed" if answered else "failed",
        "correlation_id": correlation_id,
        "task_type": routing.task_type,
        "provider": routing.provider,
        "model_name": served,
        "backend_id": result.backend_id,
        "response": answer,
        "response_chars": len(answer),
        "response_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "quality_gate_passed": model_decided,
        "quality_score": 1.0 if model_decided else 0.0,
        "error_message": "" if answered else detail,
        "metrics": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "latency_ms": result.latency_ms or 0,
            "cost_usd": 0.0,
        },
        "attempts": [
            {
                "tier": "typed_decision",
                "backend_id": result.backend_id,
                "model_id": served,
                "quality_gate_passed": model_decided,
                "acceptance_decision": "accept" if model_decided else "climb",
                "acceptance_reason": (
                    _REASON_QUALITY_MET
                    if model_decided
                    else _REASON_BELOW_BAR
                    if answered
                    else _REASON_CALL_FAILED
                ),
                "error_message": "" if answered else detail,
            }
        ],
        "attempts_count": 1,
        "escalation_count": 0,
        "session_id": correlation_id,
        "delegated_to": served,
        "repo_name": request.work_unit_repository,
        "tenant_id": routing.tenant_id,
        "emitted_at": emitted_at.isoformat(),
    }
    return ModelTypedDecisionEvent(
        topic=routing.completed_topic if answered else routing.failed_topic,
        event_type=routing.event_type,
        event_id=f"typed-decision-{correlation_id}",
        correlation_id=correlation_id,
        payload=payload,
    )


def record_typed_decision_event(
    request: ModelTypedDecisionRequest,
    result: ModelTypedDecisionResult,
    routing: ModelTypedDecisionEventRouting,
    publisher: ProtocolTypedDecisionEventPublisher,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    """Write the decision's terminal. A failure is logged, never raised."""
    try:
        event = build_typed_decision_event(request, result, routing, emitted_at=clock())
        return publisher.publish(
            topic=event.topic,
            event_type=event.event_type,
            payload=event.payload,
            event_id=event.event_id,
            correlation_id=event.correlation_id,
        )
    except Exception as exc:  # the decision must not fail on telemetry
        _LOGGER.warning(
            "typed decision event not recorded (correlation_id=%s): %s",
            result.correlation_id,
            exc,
        )
        return False


__all__: list[str] = [
    "InMemoryTypedDecisionEventPublisher",
    "ModelTypedDecisionEvent",
    "ModelTypedDecisionEventRouting",
    "ProtocolTypedDecisionEventPublisher",
    "build_typed_decision_event",
    "record_typed_decision_event",
]
