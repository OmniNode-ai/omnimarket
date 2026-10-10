# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed dispatch and preserved accumulation math, without I/O."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
from omnimarket.models.delegation.model_routing_feedback import ModelRoutingFeedback
from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers import (
    handler_delegation_routing_feedback as reducer,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_terminal_payload import (
    ModelDelegationTerminalPayload,
)

pytestmark = pytest.mark.unit
NOW = "2026-01-02T03:04:05+00:00"
WINDOW_START = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def handler(
    monkeypatch: pytest.MonkeyPatch,
) -> reducer.HandlerDelegationRoutingFeedback:
    monkeypatch.setattr(reducer, "_now_iso", lambda: NOW)
    return reducer.HandlerDelegationRoutingFeedback()


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "model_id": "model-a",
        "task_type": "test",
        "success": True,
        "latency_ms": 300,
        "correlation_id": "corr-1",
        "request_id": "req-1",
    }


@pytest.fixture
def state() -> dict[str, ModelRoutingFeedback]:
    return {
        "model-a:test": ModelRoutingFeedback(
            model_id="model-a",
            task_type="test",
            success_count=1,
            failure_count=1,
            escalation_count=1,
            total_count=2,
            success_rate=0.5,
            escalation_rate=0.5,
            avg_latency_ms=150.0,
            window_start=WINDOW_START,
            last_updated=WINDOW_START,
        ),
        "model-a:review": ModelRoutingFeedback(
            model_id="model-a", task_type="review", window_start=WINDOW_START
        ),
    }


@pytest.mark.parametrize(
    ("event_type", "success_count", "escalation_count", "source_topic"),
    [
        ("delegation-call-completed", 1, 0, DELEGATION_CALL_COMPLETED_TOPIC_V1),
        (
            "delegation-escalation-triggered",
            0,
            1,
            DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
        ),
        ("delegation-all-tiers-failed", 0, 0, DELEGATION_ALL_TIERS_FAILED_TOPIC_V1),
    ],
)
def test_explicit_event_type(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    event_type: str,
    success_count: int,
    escalation_count: int,
    source_topic: str,
) -> None:
    raw = payload | {"event_type": event_type}
    before = deepcopy(raw)
    result = handler.handle(ModelDelegationTerminalPayload(**raw))
    assert result is not None
    expected = {
        "model_id": "model-a",
        "task_type": "test",
        "success_count": success_count,
        "failure_count": 1 - success_count,
        "escalation_count": escalation_count,
        "total_count": 1,
        "success_rate": float(success_count),
        "escalation_rate": float(escalation_count),
        "avg_latency_ms": 300.0 if success_count else 0.0,
        "window_start": NOW,
        "last_updated": NOW,
    }
    assert result.feedback.model_dump() == expected
    assert result.correlation_id == "corr-1"
    assert result.source_topic == source_topic
    assert handler._state == {"model-a:test": result.feedback}
    assert raw == before


@pytest.mark.parametrize("invalid", [{"task_type": ""}, {"model_id": ""}])
def test_unidentifiable_event_preserves_state_and_emits_nothing(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, ModelRoutingFeedback],
    invalid: dict[str, Any],
) -> None:
    handler._state = state
    before = deepcopy(state)
    assert handler.handle(ModelDelegationTerminalPayload(**(payload | invalid))) is None
    assert handler._state == before


@pytest.mark.parametrize("attempted", [(), ("model-a", "")])
def test_all_failed_without_last_model_preserves_state(
    handler: reducer.HandlerDelegationRoutingFeedback,
    state: dict[str, ModelRoutingFeedback],
    attempted: tuple[str, ...],
) -> None:
    handler._state = state
    before = deepcopy(state)
    assert (
        handler.handle(
            ModelDelegationTerminalPayload(
                **{
                    "task_type": "test",
                    "event_type": "delegation-all-tiers-failed",
                    "attempted_models": attempted,
                }
            )
        )
        is None
    )
    assert handler._state == before


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("latency", [300, 300.9, 0, -1, "300", None])
def test_rehydration_latency_and_immutable_state_transition(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, ModelRoutingFeedback],
    serialized: bool,
    latency: object,
) -> None:
    prior = {k: v.model_dump() for k, v in state.items()} if serialized else state
    before = deepcopy(prior)
    event = reducer._build_feedback_event(
        ModelDelegationTerminalPayload(
            **(payload | {"success": False, "latency_ms": latency})
        )
    )
    assert event is not None
    feedback, new_state = handler.accumulate(event, prior)
    assert feedback.model_dump() == {
        **state["model-a:test"].model_dump(),
        "failure_count": 2,
        "total_count": 3,
        "success_rate": 1 / 3,
        "escalation_rate": 1 / 3,
        "avg_latency_ms": 200.0
        if isinstance(latency, int | float) and latency > 0
        else 150.0,
        "last_updated": NOW,
    }
    assert new_state["model-a:review"] == state["model-a:review"]
    assert new_state["model-a:test"] == feedback
    assert prior == before


@pytest.mark.parametrize(
    "latency", [float("nan"), float("inf"), float("-inf"), True, False]
)
def test_unusable_latency_is_ignored(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, ModelRoutingFeedback],
    latency: object,
) -> None:
    handler._state = state
    before = deepcopy(state)
    result = handler.handle(
        ModelDelegationTerminalPayload(
            **(payload | {"success": False, "latency_ms": latency})
        )
    )
    assert result is not None
    assert result.feedback.total_count == 3
    assert result.feedback.failure_count == 2
    assert result.feedback.avg_latency_ms == 150.0
    assert handler._state["model-a:test"] == result.feedback
    assert state == before


def test_contract_fsm_moves_idle_to_updated_and_keeps_updated_on_later_folds() -> None:
    """The contract's FSM declares that a folded terminal leaves the reducer updated."""
    contract_path = Path(reducer.__file__).resolve().parents[1] / "contract.yaml"
    fsm = yaml.safe_load(contract_path.read_text())["state_machine"]
    assert fsm["initial_state"] == "idle"
    assert {state["state_name"] for state in fsm["states"]} == {"idle", "updated"}
    assert {
        (transition["from_state"], transition["to_state"])
        for transition in fsm["transitions"]
    } == {("idle", "updated"), ("updated", "updated")}


@pytest.mark.parametrize(
    ("signals", "source_topic", "success_count", "escalation_count", "model_id"),
    [
        ({}, DELEGATION_CALL_COMPLETED_TOPIC_V1, 1, 0, "model-a"),
        (
            {"escalation_reason": ""},
            DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
            0,
            1,
            "model-a",
        ),
        (
            {"next_model_id": "next"},
            DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
            0,
            1,
            "model-a",
        ),
        (
            {"attempt_number": 0},
            DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
            0,
            1,
            "model-a",
        ),
        (
            {"attempted_models": ["first", "last"], "model_id": ""},
            DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
            0,
            0,
            "last",
        ),
        (
            {"attempted_models": ["first", "last"]},
            DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
            0,
            0,
            "model-a",
        ),
        (
            {"attempted_models": ["last"], "attempt_number": 1},
            DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
            0,
            0,
            "model-a",
        ),
        (
            {
                "event_type": "delegation-call-completed",
                "attempted_models": ["last"],
                "attempt_number": 1,
            },
            DELEGATION_CALL_COMPLETED_TOPIC_V1,
            1,
            0,
            "model-a",
        ),
    ],
)
def test_structural_type_precedence(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    signals: dict[str, Any],
    source_topic: str,
    success_count: int,
    escalation_count: int,
    model_id: str,
) -> None:
    result = handler.handle(ModelDelegationTerminalPayload(**(payload | signals)))
    assert result is not None
    assert result.source_topic == source_topic
    assert result.feedback.model_id == model_id
    assert result.feedback.success_count == success_count
    assert result.feedback.failure_count == 1 - success_count
    assert result.feedback.escalation_count == escalation_count
    assert result.feedback.avg_latency_ms == (300.0 if success_count else 0.0)


def test_concurrent_terminals_accumulate_without_losing_updates(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    request = ModelDelegationTerminalPayload(**payload)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(handler.handle, [request] * 64))
    assert {
        result.feedback.total_count for result in results if result is not None
    } == set(range(1, 65))
    final = handler.handle(request)
    assert final is not None
    assert final.feedback.success_count == final.feedback.total_count == 65
    assert final.feedback.success_rate == 1.0
    assert final.feedback.avg_latency_ms == 300.0
