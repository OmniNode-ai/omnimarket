# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerDelegationRoutingFeedback."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
    HandlerDelegationRoutingFeedback,
    _feedback_key,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_feedback_event import (
    EnumDelegationFeedbackEventType,
    ModelDelegationFeedbackEvent,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_routing_feedback import (
    ModelRoutingFeedback,
)


def _completed_event(
    model_id: str = "qwen3-coder-30b",
    task_type: str = "test",
    success: bool = True,
    latency_ms: int = 120,
    correlation_id: str = "corr-1",
    request_id: str = "req-1",
) -> ModelDelegationFeedbackEvent:
    return ModelDelegationFeedbackEvent(
        event_type=EnumDelegationFeedbackEventType.COMPLETED,
        correlation_id=correlation_id,
        request_id=request_id,
        task_type=task_type,
        model_id=model_id,
        success=success,
        is_escalation=False,
        latency_ms=latency_ms,
        source_topic="onex.evt.omnimarket.delegation-call-completed.v1",
    )


def _escalation_event(
    model_id: str = "qwen3-coder-30b",
    task_type: str = "test",
    correlation_id: str = "corr-2",
    request_id: str = "req-2",
) -> ModelDelegationFeedbackEvent:
    return ModelDelegationFeedbackEvent(
        event_type=EnumDelegationFeedbackEventType.ESCALATION_TRIGGERED,
        correlation_id=correlation_id,
        request_id=request_id,
        task_type=task_type,
        model_id=model_id,
        success=False,
        is_escalation=True,
        latency_ms=0,
        source_topic="onex.evt.omnimarket.delegation-escalation-triggered.v1",
    )


def _all_failed_event(
    model_id: str = "qwen3-coder-30b",
    task_type: str = "test",
    correlation_id: str = "corr-3",
    request_id: str = "req-3",
) -> ModelDelegationFeedbackEvent:
    return ModelDelegationFeedbackEvent(
        event_type=EnumDelegationFeedbackEventType.ALL_TIERS_FAILED,
        correlation_id=correlation_id,
        request_id=request_id,
        task_type=task_type,
        model_id=model_id,
        success=False,
        is_escalation=False,
        latency_ms=0,
        source_topic="onex.evt.omnimarket.delegation-all-tiers-failed.v1",
    )


@pytest.mark.unit
class TestHandlerDelegationRoutingFeedbackAccumulate:
    def test_first_success_initialises_counters(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        event = _completed_event(success=True, latency_ms=100)
        feedback, _ = handler.accumulate(event, {})

        assert feedback.total_count == 1
        assert feedback.success_count == 1
        assert feedback.failure_count == 0
        assert feedback.escalation_count == 0
        assert feedback.success_rate == pytest.approx(1.0)
        assert feedback.escalation_rate == pytest.approx(0.0)

    def test_first_failure_initialises_counters(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        event = _completed_event(success=False, latency_ms=0)
        feedback, _ = handler.accumulate(event, {})

        assert feedback.total_count == 1
        assert feedback.success_count == 0
        assert feedback.failure_count == 1
        assert feedback.success_rate == pytest.approx(0.0)

    def test_counter_accumulation_over_multiple_events(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        for i in range(3):
            ev = _completed_event(success=True, latency_ms=100, request_id=f"req-{i}")
            _, state = handler.accumulate(ev, state)
        ev_fail = _completed_event(success=False, latency_ms=0, request_id="req-fail")
        feedback, _ = handler.accumulate(ev_fail, state)

        assert feedback.total_count == 4
        assert feedback.success_count == 3
        assert feedback.failure_count == 1
        assert feedback.success_rate == pytest.approx(0.75)

    def test_escalation_event_increments_escalation_count(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        ev_ok = _completed_event(success=True, latency_ms=80)
        _, state = handler.accumulate(ev_ok, state)
        ev_esc = _escalation_event()
        feedback, _ = handler.accumulate(ev_esc, state)

        assert feedback.escalation_count == 1
        assert feedback.escalation_rate == pytest.approx(0.5)
        assert feedback.total_count == 2

    def test_all_tiers_failed_increments_failure(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        ev = _all_failed_event()
        feedback, _ = handler.accumulate(ev, {})

        assert feedback.failure_count == 1
        assert feedback.success_count == 0
        assert feedback.escalation_count == 0

    def test_avg_latency_incremental_average(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        for latency in (100, 200, 300):
            ev = _completed_event(
                success=True,
                latency_ms=latency,
                request_id=f"req-{latency}",
            )
            _, state = handler.accumulate(ev, state)

        key = _feedback_key("qwen3-coder-30b", "test")
        final = state[key]
        assert isinstance(final, ModelRoutingFeedback)
        assert final.avg_latency_ms == pytest.approx(200.0)

    def test_zero_latency_events_do_not_skew_average(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        ev1 = _completed_event(success=True, latency_ms=100, request_id="r1")
        _, state = handler.accumulate(ev1, state)
        ev_esc = _escalation_event()
        feedback, state = handler.accumulate(ev_esc, state)
        # latency average should stay at 100 — escalation event has latency=0
        # but is NOT a COMPLETED event so avg should not change
        # (incremental avg formula: prev_total / new_total would drop it)
        # The handler only updates avg_latency for COMPLETED events with latency_ms > 0
        assert feedback.avg_latency_ms == pytest.approx(100.0)

    def test_state_is_isolated_per_model_task_pair(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        ev_a = _completed_event(model_id="model-a", task_type="test", success=True)
        _, state = handler.accumulate(ev_a, state)
        ev_b = _completed_event(model_id="model-b", task_type="test", success=False)
        _, state = handler.accumulate(ev_b, state)

        key_a = _feedback_key("model-a", "test")
        key_b = _feedback_key("model-b", "test")
        fb_a = state[key_a]
        fb_b = state[key_b]
        assert isinstance(fb_a, ModelRoutingFeedback)
        assert isinstance(fb_b, ModelRoutingFeedback)
        assert fb_a.success_count == 1
        assert fb_b.failure_count == 1

    def test_window_start_preserved_across_accumulations(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        ev1 = _completed_event(success=True, request_id="r1")
        fb1, state = handler.accumulate(ev1, state)
        window_start = fb1.window_start

        ev2 = _completed_event(success=True, request_id="r2")
        fb2, _ = handler.accumulate(ev2, state)
        assert fb2.window_start == window_start

    def test_idempotent_model_is_frozen(self) -> None:
        fb = ModelRoutingFeedback(
            model_id="m",
            task_type="t",
            success_count=1,
            total_count=1,
            success_rate=1.0,
        )
        with pytest.raises(ValidationError):
            fb.success_count = 2  # type: ignore[misc]


def _completed_payload(
    *,
    model_id: str = "deepseek-r1-14b",
    task_type: str = "code_review",
    success: bool = True,
    latency_ms: int = 250,
    correlation_id: str = "c1",
    request_id: str = "r1",
) -> dict[str, Any]:
    """A raw delegation-call-completed payload as the live publisher emits it.

    The ModelLlmDelegationCompletedEvent shape carries many cost/provenance
    fields the feedback reducer ignores; only model_id, task_type, success,
    latency_ms, and correlation/request ids are consumed.
    """
    return {
        "correlation_id": correlation_id,
        "causation_id": "cause-1",
        "request_id": request_id,
        "task_type": task_type,
        "task_id": None,
        "selected_model": model_id,
        "model_id": model_id,
        "model_tier": "cheap_local",
        "provider": "bifrost",
        "endpoint_ref": "BIFROST_URL",
        "tokens_in": 100,
        "tokens_out": 50,
        "latency_ms": latency_ms,
        "success": success,
        "escalated_to": None,
    }


def _escalation_payload(
    *,
    model_id: str = "ds-v4-flash",
    task_type: str = "codegen",
    correlation_id: str = "04d63eb7-be92-4f7a-b4c8-5bcdce043a9d",
    request_id: str = "req-esc",
) -> dict[str, Any]:
    """A raw delegation-escalation-triggered payload from the live publisher."""
    return {
        "correlation_id": correlation_id,
        "causation_id": "cause-esc",
        "request_id": request_id,
        "task_type": task_type,
        "task_id": None,
        "model_id": model_id,
        "attempt_number": 1,
        "escalation_reason": "timed out",
        "next_model_id": "claude",
    }


def _all_tiers_failed_payload(
    *,
    task_type: str = "codegen",
    attempted_models: tuple[str, ...] = ("ds-v4-flash", "qwen3-coder-30b", "claude"),
    correlation_id: str = "corr-failed",
    request_id: str = "req-failed",
) -> dict[str, Any]:
    """A raw delegation-all-tiers-failed payload from the live publisher."""
    return {
        "correlation_id": correlation_id,
        "causation_id": "cause-failed",
        "request_id": request_id,
        "task_type": task_type,
        "task_id": None,
        "attempted_models": attempted_models,
    }


@pytest.mark.unit
class TestHandlerRealDispatchPath:
    """Drive handle() with the payload shapes the live auto-wiring dispatcher
    delivers (OMN-13216): the raw terminal-event payload, flattened, with a
    ``_topic`` marker — NOT a hand-crafted {"event": ..., "state": ...} wrapper.
    """

    def test_empty_payload_is_noop_not_crash(self) -> None:
        # Regression: ModelDelegationFeedbackEvent(**{}) used to raise a
        # 7-field ValidationError out of the dispatcher and swallow the terminal.
        handler = HandlerDelegationRoutingFeedback()
        result = handler.handle({})
        assert result["skipped"] is True
        assert result["feedback"] is None
        assert result["event"] is None
        assert result["state"] == {}

    def test_completed_raw_payload_with_topic_marker(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        payload = _completed_payload(success=True, latency_ms=250)
        payload["_topic"] = DELEGATION_CALL_COMPLETED_TOPIC_V1
        result = handler.handle(payload)

        assert result["skipped"] is False
        assert result["feedback"]["success_count"] == 1
        assert result["feedback"]["total_count"] == 1
        assert result["feedback"]["avg_latency_ms"] == pytest.approx(250.0)
        assert result["event"]["source_topic"] == DELEGATION_CALL_COMPLETED_TOPIC_V1

    def test_escalation_raw_payload_emits_terminal_not_swallowed(self) -> None:
        # The ticket's failing cell: an escalated request whose terminal was
        # swallowed. With the normalizing handler it produces a feedback update.
        handler = HandlerDelegationRoutingFeedback()
        payload = _escalation_payload(model_id="ds-v4-flash", task_type="codegen")
        payload["_topic"] = DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1
        result = handler.handle(payload)

        assert result["skipped"] is False
        assert result["feedback"]["escalation_count"] == 1
        assert result["feedback"]["escalation_rate"] == pytest.approx(1.0)
        assert (
            result["event"]["source_topic"] == DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1
        )

    def test_all_tiers_failed_attributes_to_last_attempted_model(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        payload = _all_tiers_failed_payload(
            attempted_models=("ds-v4-flash", "qwen3-coder-30b", "claude"),
            task_type="codegen",
        )
        payload["_topic"] = DELEGATION_ALL_TIERS_FAILED_TOPIC_V1
        result = handler.handle(payload)

        assert result["skipped"] is False
        assert result["feedback"]["model_id"] == "claude"
        assert result["feedback"]["failure_count"] == 1
        assert result["feedback"]["success_count"] == 0

    def test_model_event_envelope_input_is_unwrapped(self) -> None:
        # The runtime delivers a ModelEventEnvelope when the handler's
        # event_model is unset; the handler must unwrap envelope.payload.
        from omnibase_core.models.events.model_event_envelope import (
            ModelEventEnvelope,
        )

        handler = HandlerDelegationRoutingFeedback()
        payload = _completed_payload(success=True, latency_ms=120)
        payload["source_topic"] = DELEGATION_CALL_COMPLETED_TOPIC_V1
        envelope = ModelEventEnvelope[dict[str, Any]](
            payload=payload,
            event_type=DELEGATION_CALL_COMPLETED_TOPIC_V1,
            correlation_id=uuid4(),
        )
        result = handler.handle(envelope)

        assert result["skipped"] is False
        assert result["feedback"]["total_count"] == 1
        assert result["feedback"]["success_count"] == 1

    def test_transport_envelope_dict_is_unwrapped(self) -> None:
        # Double-wrapped transport envelope: {"payload": {domain}, markers...}.
        handler = HandlerDelegationRoutingFeedback()
        domain = _completed_payload(success=False, latency_ms=0)
        result = handler.handle(
            {
                "payload": domain,
                "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
                "partition_key": None,
            }
        )
        assert result["skipped"] is False
        assert result["feedback"]["failure_count"] == 1

    def test_state_accumulates_across_dispatch_invocations(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        p1 = _completed_payload(
            model_id="qwen3-30b",
            task_type="test",
            success=True,
            latency_ms=100,
            request_id="r1",
        )
        p1["_topic"] = DELEGATION_CALL_COMPLETED_TOPIC_V1
        r1 = handler.handle(p1)

        p2 = _completed_payload(
            model_id="qwen3-30b",
            task_type="test",
            success=False,
            latency_ms=0,
            request_id="r2",
        )
        p2["_topic"] = DELEGATION_CALL_COMPLETED_TOPIC_V1
        p2["_state"] = r1["state"]
        r2 = handler.handle(p2)

        assert r2["feedback"]["total_count"] == 2
        assert r2["feedback"]["success_rate"] == pytest.approx(0.5)

    def test_unknown_topic_payload_is_noop(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        result = handler.handle(
            {"_topic": "onex.evt.omnimarket.something-else.v1", "model_id": "m"}
        )
        assert result["skipped"] is True
        assert result["feedback"] is None

    def test_missing_model_id_is_noop(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        result = handler.handle(
            {
                "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
                "task_type": "test",
                "success": True,
            }
        )
        assert result["skipped"] is True
        assert result["feedback"] is None
