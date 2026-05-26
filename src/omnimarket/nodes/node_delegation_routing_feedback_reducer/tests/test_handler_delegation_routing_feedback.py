# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerDelegationRoutingFeedback."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

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


@pytest.mark.unit
class TestHandlerDelegationRoutingFeedbackHandleShim:
    def test_handle_returns_feedback_state_and_event(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        result = handler.handle(
            {
                "event": {
                    "event_type": "delegation-call-completed",
                    "correlation_id": "c1",
                    "request_id": "r1",
                    "task_type": "code_review",
                    "model_id": "deepseek-r1-14b",
                    "success": True,
                    "is_escalation": False,
                    "latency_ms": 250,
                    "source_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
                },
                "state": {},
            }
        )
        assert "feedback" in result
        assert "state" in result
        assert "event" in result
        assert result["feedback"]["success_count"] == 1
        assert result["feedback"]["total_count"] == 1
        assert (
            result["event"]["source_topic"]
            == "onex.evt.omnimarket.delegation-call-completed.v1"
        )

    def test_handle_accumulates_into_provided_state(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        r1 = handler.handle(
            {
                "event": {
                    "event_type": "delegation-call-completed",
                    "correlation_id": "c1",
                    "request_id": "r1",
                    "task_type": "test",
                    "model_id": "qwen3-30b",
                    "success": True,
                    "is_escalation": False,
                    "latency_ms": 100,
                    "source_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
                },
                "state": {},
            }
        )
        r2 = handler.handle(
            {
                "event": {
                    "event_type": "delegation-call-completed",
                    "correlation_id": "c2",
                    "request_id": "r2",
                    "task_type": "test",
                    "model_id": "qwen3-30b",
                    "success": False,
                    "is_escalation": False,
                    "latency_ms": 0,
                    "source_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
                },
                "state": r1["state"],
            }
        )
        assert r2["feedback"]["total_count"] == 2
        assert r2["feedback"]["success_rate"] == pytest.approx(0.5)
