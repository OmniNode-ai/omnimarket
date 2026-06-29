# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationRoutingFeedback — accumulate terminal events into routing feedback.

Consumes delegation terminal events from three topics:
  - delegation-call-completed.v1      → success or failure signal + latency
  - delegation-escalation-triggered.v1 → escalation signal per model
  - delegation-all-tiers-failed.v1    → failure signal for the last attempted model

State is kept in a plain dict[str, ModelRoutingFeedback] keyed by
"(model_id):(task_type)". The caller is responsible for persisting and
rehydrating this state between invocations (passed in via the ``_state`` marker
on the dispatch input, or the legacy ``state`` key).

Idempotency: callers must deduplicate on correlation_id:request_id before
passing events into this handler. The handler itself is a pure accumulator.

Dispatch input shape (OMN-13216): the runtime auto-wiring delivers the raw
terminal-event payload to ``handle`` — either as a ``ModelEventEnvelope``, a
transport-envelope dict, or the flattened payload dict — NOT a hand-crafted
``{"event": {...}, "state": {...}}`` wrapper. The previous shim read
``input_data["event"]`` which the live dispatcher never populates, so it
constructed ``ModelDelegationFeedbackEvent`` from ``{}`` and raised a 7-field
ValidationError. That crash propagated out of the dispatcher and swallowed the
terminal for escalated requests (request HWM advanced, terminal HWM did not).
``handle`` now extracts and normalizes the real payload, and treats an
empty/unidentifiable payload as an explicit no-op rather than crashing.

Related:
    - OMN-12129: Routing feedback loop — runtime success/failure rates
    - OMN-13216: Reducer crashed on empty payload, swallowing escalated terminals
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
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

# Runtime auto-wiring markers injected alongside the domain payload. These are
# not part of any terminal-event schema and must be stripped before the payload
# is validated into a domain model.
_DISPATCH_MARKERS = frozenset(
    {
        "_db",
        "_event_type",
        "_topic",
        "_partition",
        "_offset",
        "_terminal_event_id",
        "_state",
        # Transport-envelope metadata that may ride alongside a flattened payload.
        "envelope_id",
        "envelope_timestamp",
        "partition_key",
        "__debug_trace",
    }
)

# Map a subscribed terminal topic to the normalized feedback event type.
_TOPIC_TO_EVENT_TYPE: dict[str, EnumDelegationFeedbackEventType] = {
    DELEGATION_CALL_COMPLETED_TOPIC_V1: EnumDelegationFeedbackEventType.COMPLETED,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1: (
        EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED
    ),
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1: (
        EnumDelegationFeedbackEventType.ALL_TIERS_FAILED
    ),
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


def _as_mapping(candidate: object) -> Mapping[str, Any] | None:
    """Return a string-keyed mapping view of ``candidate`` if one is available."""
    if isinstance(candidate, Mapping):
        return candidate
    model_dump = getattr(candidate, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _unwrap_envelope(candidate: object) -> object:
    """Recursively unwrap transport/event envelopes to reach the domain payload.

    The runtime may deliver a ``ModelEventEnvelope`` or a (possibly nested)
    transport-envelope dict ``{"payload": {...}, ...markers}``. Mirrors the
    runtime's own ``_extract_dispatch_payload`` unwrap so the handler sees the
    domain payload regardless of how deeply it was wrapped.
    """
    current = candidate
    for _ in range(8):  # bounded: guards against pathological self-references
        mapping = _as_mapping(current)
        if mapping is None:
            return current
        if "payload" not in mapping:
            return current
        # A transport/event envelope carries a "payload" plus envelope markers.
        # A domain terminal event does not declare a "payload" field, so the
        # presence of "payload" identifies a wrapper to unwrap.
        current = mapping["payload"]
    return current


def _strip_markers(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _DISPATCH_MARKERS}


def _resolve_source_topic(
    raw_input: object,
    payload: Mapping[str, Any],
) -> str:
    """Best-effort resolution of the source topic from the dispatch input."""
    raw_mapping = _as_mapping(raw_input)
    if raw_mapping is not None:
        for key in ("_topic", "topic"):
            value = raw_mapping.get(key)
            if isinstance(value, str) and value:
                return value
        debug_trace = raw_mapping.get("__debug_trace")
        if isinstance(debug_trace, Mapping):
            topic = debug_trace.get("topic")
            if isinstance(topic, str) and topic:
                return topic
        event_type = raw_mapping.get("event_type")
        if isinstance(event_type, str) and event_type in _TOPIC_TO_EVENT_TYPE:
            return event_type
    topic_attr = getattr(raw_input, "topic", None)
    if isinstance(topic_attr, str) and topic_attr:
        return topic_attr
    source_topic = payload.get("source_topic")
    if isinstance(source_topic, str) and source_topic:
        return source_topic
    return ""


def _resolve_state(raw_input: object) -> dict[str, Any]:
    raw_mapping = _as_mapping(raw_input)
    if raw_mapping is None:
        return {}
    for key in ("_state", "state"):
        value = raw_mapping.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _resolve_model_id(
    payload: Mapping[str, Any],
    event_type: EnumDelegationFeedbackEventType,
) -> str:
    """Resolve the model_id to attribute this terminal event to.

    For all-tiers-failed there is no single ``model_id``; the last attempted
    model is the one whose ceiling failed, so it carries the failure signal.
    """
    model_id = payload.get("model_id")
    if isinstance(model_id, str) and model_id:
        return model_id
    if event_type == EnumDelegationFeedbackEventType.ALL_TIERS_FAILED:
        attempted = payload.get("attempted_models")
        if isinstance(attempted, list | tuple) and attempted:
            last = attempted[-1]
            if isinstance(last, str) and last:
                return last
    return ""


def _resolve_event_type(
    payload: Mapping[str, Any],
    source_topic: str,
) -> EnumDelegationFeedbackEventType | None:
    event_type = _TOPIC_TO_EVENT_TYPE.get(source_topic)
    if event_type is not None:
        return event_type
    # Topic is unknown — fall back to the payload's own event_type if it names
    # one of the known feedback event types.
    raw_event_type = payload.get("event_type")
    if isinstance(raw_event_type, str):
        try:
            return EnumDelegationFeedbackEventType(raw_event_type)
        except ValueError:
            return None
    return None


def _build_feedback_event(
    payload: Mapping[str, Any],
    source_topic: str,
) -> ModelDelegationFeedbackEvent | None:
    """Normalize a raw terminal-event payload into a ``ModelDelegationFeedbackEvent``.

    Returns ``None`` when the payload is empty or lacks the minimal identity
    fields required to attribute feedback — the caller treats this as a no-op so
    a malformed/empty message does not crash the dispatcher and swallow terminals.
    """
    if not payload:
        return None

    event_type = _resolve_event_type(payload, source_topic)
    if event_type is None:
        logger.warning(
            "Delegation feedback: unrecognized source topic %r and event_type "
            "%r — skipping (no-op, terminal preserved)",
            source_topic,
            payload.get("event_type"),
        )
        return None

    task_type = payload.get("task_type")
    if not isinstance(task_type, str) or not task_type:
        logger.warning(
            "Delegation feedback: missing task_type for %s — skipping (no-op)",
            event_type.value,
        )
        return None

    model_id = _resolve_model_id(payload, event_type)
    if not model_id:
        logger.warning(
            "Delegation feedback: missing model_id for %s task=%s — skipping (no-op)",
            event_type.value,
            task_type,
        )
        return None

    correlation_id = str(payload.get("correlation_id") or "")
    request_id = str(payload.get("request_id") or "")

    if event_type == EnumDelegationFeedbackEventType.COMPLETED:
        success = bool(payload.get("success", False))
        is_escalation = False
        latency_raw = payload.get("latency_ms", 0)
        latency_ms = int(latency_raw) if isinstance(latency_raw, int | float) else 0
    elif event_type == EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED:
        success = False
        is_escalation = True
        latency_ms = 0
    else:  # ALL_TIERS_FAILED
        success = False
        is_escalation = False
        latency_ms = 0

    return ModelDelegationFeedbackEvent(
        event_type=event_type,
        correlation_id=correlation_id,
        request_id=request_id,
        task_type=task_type,
        model_id=model_id,
        success=success,
        is_escalation=is_escalation,
        latency_ms=latency_ms,
        source_topic=source_topic,
    )


class HandlerDelegationRoutingFeedback:
    """Pure reducer: accumulate terminal event -> updated ModelRoutingFeedback."""

    def handle(self, input_data: object) -> dict[str, Any]:
        """Runtime dispatch entrypoint.

        ``input_data`` is whatever the auto-wiring dispatcher delivers: a
        ``ModelEventEnvelope``, a transport-envelope dict, or the flattened
        domain payload dict — optionally carrying runtime markers (``_topic``,
        ``_state``, ...). The handler extracts the domain payload, normalizes it
        into a ``ModelDelegationFeedbackEvent`` keyed by source topic, and
        accumulates it into the prior state.

        Returns:
          - feedback: updated ModelRoutingFeedback (or null on no-op)
          - state: updated full state dict
          - event: ModelRoutingFeedbackUpdatedEvent as dict (or null on no-op)
          - skipped: True when the payload was empty/unidentifiable (no-op)
        """
        state = _resolve_state(input_data)
        domain = _unwrap_envelope(input_data)
        domain_mapping = _as_mapping(domain)
        if domain_mapping is None:
            logger.warning(
                "Delegation feedback: dispatch payload of type %s is not a "
                "mapping — skipping (no-op, terminal preserved)",
                type(domain).__name__,
            )
            return self._noop_result(state)

        payload = _strip_markers(domain_mapping)
        source_topic = _resolve_source_topic(input_data, payload)
        event = _build_feedback_event(payload, source_topic)
        if event is None:
            return self._noop_result(state)

        updated_feedback, new_state = self.accumulate(event, state)

        output_event = ModelRoutingFeedbackUpdatedEvent(
            correlation_id=event.correlation_id,
            feedback=updated_feedback,
            source_topic=event.source_topic,
        )

        return {
            "feedback": updated_feedback.model_dump(mode="json"),
            "state": {k: v.model_dump(mode="json") for k, v in new_state.items()},
            "event": output_event.model_dump(mode="json"),
            "skipped": False,
        }

    @staticmethod
    def _noop_result(state: dict[str, Any]) -> dict[str, Any]:
        """Return a no-op dispatch result that preserves state and the terminal.

        The dispatcher does not crash, so the source terminal event is not
        swallowed; request HWM and terminal HWM stay aligned (OMN-13216).
        """
        normalized_state = {
            k: (v.model_dump(mode="json") if isinstance(v, ModelRoutingFeedback) else v)
            for k, v in state.items()
        }
        return {
            "feedback": None,
            "state": normalized_state,
            "event": None,
            "skipped": True,
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
