# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accumulate terminal events into routing feedback.

State is process-local per window (window_start), keyed by model and task type.
The consumer orders by window and count so a new process window replaces an old
one even when its count is lower. Callers must deduplicate terminal identities
before accumulation.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
from omnimarket.models.delegation.model_routing_feedback import (
    ModelRoutingFeedback,
    ModelRoutingFeedbackUpdatedEvent,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_feedback_event import (
    EnumDelegationFeedbackEventType,
    ModelDelegationFeedbackEvent,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_terminal_payload import (
    ModelDelegationTerminalPayload,
)

logger = logging.getLogger(__name__)
_FEEDBACK_KEY_SEP = ":"
_EVENT_TYPE_TO_TOPIC = {
    EnumDelegationFeedbackEventType.COMPLETED: DELEGATION_CALL_COMPLETED_TOPIC_V1,
    EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED: DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
    EnumDelegationFeedbackEventType.ALL_TIERS_FAILED: DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
}


def _feedback_key(model_id: str, task_type: str) -> str:
    return f"{model_id}{_FEEDBACK_KEY_SEP}{task_type}"


def _compute_rates(
    success_count: int,
    escalation_count: int,
    total_count: int,
) -> tuple[float, float]:
    if total_count == 0:
        return 0.0, 0.0
    return success_count / total_count, escalation_count / total_count


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _build_feedback_event(
    request: ModelDelegationTerminalPayload,
) -> ModelDelegationFeedbackEvent | None:
    event_type = request.event_type
    if event_type is None:
        if request.attempted_models:
            event_type = EnumDelegationFeedbackEventType.ALL_TIERS_FAILED
        elif any(
            value is not None
            for value in (
                request.escalation_reason,
                request.next_model_id,
                request.attempt_number,
            )
        ):
            event_type = EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED
        else:
            event_type = EnumDelegationFeedbackEventType.COMPLETED

    if not request.task_type:
        logger.warning(
            "Delegation feedback: missing task_type for %s — skipping (no-op)",
            event_type.value,
        )
        return None
    model_id = request.model_id
    if (
        not model_id
        and event_type == EnumDelegationFeedbackEventType.ALL_TIERS_FAILED
        and request.attempted_models
    ):
        model_id = request.attempted_models[-1]
    if not model_id:
        logger.warning(
            "Delegation feedback: missing model_id for %s task=%s — skipping (no-op)",
            event_type.value,
            request.task_type,
        )
        return None

    completed = event_type == EnumDelegationFeedbackEventType.COMPLETED
    latency_raw = request.latency_ms
    latency_ms = (
        int(latency_raw)
        if completed
        and isinstance(latency_raw, int | float)
        and not isinstance(latency_raw, bool)
        and math.isfinite(latency_raw)
        else 0
    )
    return ModelDelegationFeedbackEvent(
        event_type=event_type,
        correlation_id=request.correlation_id,
        request_id=request.request_id,
        task_type=request.task_type,
        model_id=model_id,
        success=request.success if completed else False,
        is_escalation=event_type
        == EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED,
        latency_ms=latency_ms,
        source_topic=_EVENT_TYPE_TO_TOPIC[event_type],
    )


class HandlerDelegationRoutingFeedback:
    """Accumulate typed terminal events in one long-lived runtime instance."""

    def __init__(self) -> None:
        self._state: dict[str, ModelRoutingFeedback] = {}
        self._lock = Lock()

    def handle(
        self, request: ModelDelegationTerminalPayload
    ) -> ModelRoutingFeedbackUpdatedEvent | None:
        """Return the cumulative update for publication, or an identity no-op."""
        event = _build_feedback_event(request)
        if event is None:
            return None
        with self._lock:
            updated, new_state = self.accumulate(event, self._state)
            self._state = new_state
        return ModelRoutingFeedbackUpdatedEvent(
            correlation_id=event.correlation_id,
            feedback=updated,
            source_topic=event.source_topic,
        )

    def accumulate(
        self,
        event: ModelDelegationFeedbackEvent,
        state: dict[str, Any],
    ) -> tuple[ModelRoutingFeedback, dict[str, ModelRoutingFeedback]]:
        """Accumulate one terminal event into the feedback state.

        For all-tiers-failed events the model_id in the normalized event
        represents the last attempted model. Callers that want per-model
        tracking across all attempted models should call accumulate() once
        per attempted model.

        Returns:
            Tuple of (updated feedback for this model_id+task_type, full new state).
        """
        key = _feedback_key(event.model_id, event.task_type)
        now = _now_iso()

        # Deserialize existing feedback if present (accept dict or model instance)
        existing_raw = state.get(key)
        if isinstance(existing_raw, ModelRoutingFeedback):
            existing = existing_raw
        elif existing_raw:
            existing = ModelRoutingFeedback(**existing_raw)
        else:
            existing = ModelRoutingFeedback(
                model_id=event.model_id,
                task_type=event.task_type,
                window_start=now,
            )

        # Accumulate counters
        new_total = existing.total_count + 1
        new_success = existing.success_count + (1 if event.success else 0)
        new_failure = existing.failure_count + (0 if event.success else 1)
        new_escalation = existing.escalation_count + (1 if event.is_escalation else 0)

        # Incremental average latency (only for completed events carrying actual latency)
        if (
            event.event_type == EnumDelegationFeedbackEventType.COMPLETED
            and event.latency_ms > 0
        ):
            prev_latency_total = existing.avg_latency_ms * existing.total_count
            new_avg_latency = (prev_latency_total + event.latency_ms) / new_total
        else:
            new_avg_latency = existing.avg_latency_ms

        success_rate, escalation_rate = _compute_rates(
            new_success, new_escalation, new_total
        )

        updated = ModelRoutingFeedback(
            model_id=event.model_id,
            task_type=event.task_type,
            success_count=new_success,
            failure_count=new_failure,
            escalation_count=new_escalation,
            total_count=new_total,
            success_rate=success_rate,
            escalation_rate=escalation_rate,
            avg_latency_ms=new_avg_latency,
            window_start=existing.window_start,
            last_updated=now,
        )

        logger.debug(
            "Feedback accumulated: model=%s task=%s total=%d success_rate=%.3f",
            event.model_id,
            event.task_type,
            new_total,
            success_rate,
        )

        new_state: dict[str, ModelRoutingFeedback] = {
            k: (v if isinstance(v, ModelRoutingFeedback) else ModelRoutingFeedback(**v))
            for k, v in state.items()
        }
        new_state[key] = updated

        return updated, new_state


__all__ = ["HandlerDelegationRoutingFeedback"]
