# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationRoutingFeedback — accumulate terminal events into routing feedback.

Consumes delegation terminal events from three topics:
  - delegation-call-completed.v1      → success or failure signal + latency
  - delegation-escalation-triggered.v1 → escalation signal per model
  - delegation-all-tiers-failed.v1    → failure signal for each attempted model

State is kept in a plain dict[str, ModelRoutingFeedback] keyed by
"(model_id):(task_type)". The caller is responsible for persisting and
rehydrating this state between invocations (passed in via input_data["state"]).

Idempotency: callers must deduplicate on correlation_id:request_id before
passing events into this handler. The handler itself is a pure accumulator.

Related:
    - OMN-12129: Routing feedback loop — runtime success/failure rates
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_feedback_event import (
    EnumDelegationFeedbackEventType,
    ModelDelegationFeedbackEvent,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_routing_feedback import (
    ModelRoutingFeedback,
    ModelRoutingFeedbackUpdatedEvent,
)

logger = logging.getLogger(__name__)

_FEEDBACK_KEY_SEP = ":"


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


class HandlerDelegationRoutingFeedback:
    """Pure reducer: accumulate terminal event -> updated ModelRoutingFeedback."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler shim.

        Expected keys in input_data:
          - event: dict matching ModelDelegationFeedbackEvent
          - state: dict[str, dict] — serialized feedback map (may be empty)

        Returns:
          - feedback: updated ModelRoutingFeedback for the affected (model_id, task_type)
          - state: updated full state dict
          - event: ModelRoutingFeedbackUpdatedEvent as dict
        """
        raw_event = input_data.get("event", {})
        raw_state: dict[str, Any] = dict(input_data.get("state", {}))

        event = ModelDelegationFeedbackEvent(**raw_event)
        updated_feedback, new_state = self.accumulate(event, raw_state)

        output_event = ModelRoutingFeedbackUpdatedEvent(
            correlation_id=event.correlation_id,
            feedback=updated_feedback,
            source_topic=event.source_topic,
        )

        return {
            "feedback": updated_feedback.model_dump(mode="json"),
            "state": {k: v.model_dump(mode="json") for k, v in new_state.items()},
            "event": output_event.model_dump(mode="json"),
        }

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
