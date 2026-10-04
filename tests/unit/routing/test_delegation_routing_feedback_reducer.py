# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic feedback transitions at the dispatch boundary, without I/O."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers import (
    handler_delegation_routing_feedback as reducer,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models import (
    ModelRoutingFeedback,
)

pytestmark = pytest.mark.unit

NOW = "2026-01-02T03:04:05+00:00"
WINDOW_START = "2026-01-01T00:00:00+00:00"


@dataclass
class TopicEnvelope:
    payload: object
    topic: str = DELEGATION_CALL_COMPLETED_TOPIC_V1

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"payload": self.payload}


class NonMappingDump:
    def model_dump(self, *, mode: str) -> list[object]:
        assert mode == "json"
        return []


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
def state() -> dict[str, Any]:
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
        ).model_dump(mode="json"),
    }


def _serialized(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.model_dump(mode="json")
        if isinstance(value, ModelRoutingFeedback)
        else deepcopy(value)
        for key, value in state.items()
    }


def _assert_update(
    result: dict[str, Any], *, source_topic: str, success: bool = True
) -> None:
    feedback = {
        "model_id": "model-a",
        "task_type": "test",
        "success_count": int(success),
        "failure_count": int(not success),
        "escalation_count": 0,
        "total_count": 1,
        "success_rate": float(success),
        "escalation_rate": 0.0,
        "avg_latency_ms": 300.0,
        "window_start": NOW,
        "last_updated": NOW,
    }
    assert result == {
        "feedback": feedback,
        "state": {"model-a:test": feedback},
        "event": {
            "correlation_id": "corr-1",
            "feedback": feedback,
            "source_topic": source_topic,
        },
        "skipped": False,
    }


@pytest.mark.parametrize(
    "markers",
    [
        {"topic": DELEGATION_CALL_COMPLETED_TOPIC_V1},
        {"__debug_trace": {"topic": DELEGATION_CALL_COMPLETED_TOPIC_V1}},
        {"event_type": DELEGATION_CALL_COMPLETED_TOPIC_V1},
        {"source_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1},
        {
            "_topic": None,
            "topic": "",
            "__debug_trace": {"topic": 123},
            "source_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        },
    ],
    ids=[
        "topic",
        "debug-trace",
        "transport-event-type",
        "source-topic",
        "invalid-markers",
    ],
)
def test_topic_fallbacks_emit_feedback(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    markers: dict[str, Any],
) -> None:
    dispatch = {**payload, **markers}
    before = deepcopy(dispatch)
    _assert_update(
        handler.handle(dispatch), source_topic=DELEGATION_CALL_COMPLETED_TOPIC_V1
    )
    assert dispatch == before


def test_topic_attribute_on_model_envelope(
    handler: reducer.HandlerDelegationRoutingFeedback, payload: dict[str, Any]
) -> None:
    before = deepcopy(payload)
    _assert_update(
        handler.handle(TopicEnvelope(payload)),
        source_topic=DELEGATION_CALL_COMPLETED_TOPIC_V1,
    )
    assert payload == before


@pytest.mark.parametrize("source_topic", ["", "unknown-topic"])
@pytest.mark.parametrize(
    ("event_type", "success_count", "escalation_count"),
    [
        ("delegation-call-completed", 1, 0),
        ("delegation-escalation-triggered", 0, 1),
        ("delegation-all-tiers-failed", 0, 0),
    ],
)
def test_domain_event_type_fallback(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    source_topic: str,
    event_type: str,
    success_count: int,
    escalation_count: int,
) -> None:
    dispatch = {**payload, "event_type": event_type, "_topic": source_topic}
    before = deepcopy(dispatch)
    result = handler.handle(dispatch)
    feedback = result["feedback"]
    assert feedback == {
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
    assert result["state"] == {"model-a:test": feedback}
    assert result["event"] == {
        "correlation_id": "corr-1",
        "feedback": feedback,
        "source_topic": source_topic,
    }
    assert result["skipped"] is False
    assert dispatch == before


def test_source_topic_precedence_over_conflicting_event_type(
    handler: reducer.HandlerDelegationRoutingFeedback, payload: dict[str, Any]
) -> None:
    dispatch = {
        **payload,
        "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        "topic": DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
        "__debug_trace": {"topic": DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1},
        "source_topic": DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
        "event_type": "delegation-escalation-triggered",
    }
    _assert_update(
        handler.handle(dispatch), source_topic=DELEGATION_CALL_COMPLETED_TOPIC_V1
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"event_type": "unrecognized", "_topic": "unknown-topic"},
        {"event_type": 123, "_topic": "unknown-topic"},
        {"task_type": None},
        {"task_type": ""},
        {"task_type": 123},
        {"model_id": None},
        {"model_id": ""},
        {"model_id": 123},
    ],
)
def test_unidentifiable_event_preserves_state_and_emits_nothing(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
    invalid: dict[str, Any],
) -> None:
    before = deepcopy(state)
    dispatch = {
        **payload,
        "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        "_state": state,
        **invalid,
    }
    assert handler.handle(dispatch) == {
        "feedback": None,
        "state": _serialized(before),
        "event": None,
        "skipped": True,
    }
    assert state == before


@pytest.mark.parametrize("domain", [None, 123, "invalid", [], NonMappingDump()])
def test_non_mapping_envelope_payload_is_noop(
    handler: reducer.HandlerDelegationRoutingFeedback,
    state: dict[str, Any],
    domain: object,
) -> None:
    before = deepcopy(state)
    result = handler.handle({"payload": domain, "_state": state})
    assert result == {
        "feedback": None,
        "state": _serialized(before),
        "event": None,
        "skipped": True,
    }
    assert state == before


def test_non_mapping_dispatch_is_noop(
    handler: reducer.HandlerDelegationRoutingFeedback,
) -> None:
    assert handler.handle(NonMappingDump()) == {
        "feedback": None,
        "state": {},
        "event": None,
        "skipped": True,
    }


@pytest.mark.parametrize("depth", [8, 9])
def test_envelope_unwrapping_is_bounded(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
    depth: int,
) -> None:
    before = deepcopy(state)
    domain: dict[str, Any] = payload
    for _ in range(depth):
        domain = {"payload": domain}
    domain["_topic"] = DELEGATION_CALL_COMPLETED_TOPIC_V1
    domain["_state"] = state
    result = handler.handle(domain)
    if depth == 8:
        assert result["feedback"]["total_count"] == 3
        assert result["state"]["model-a:review"] == before["model-a:review"]
        assert result["state"]["model-a:test"] == result["feedback"]
        assert result["event"] == {
            "correlation_id": "corr-1",
            "feedback": result["feedback"],
            "source_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        }
        assert result["skipped"] is False
    else:
        assert result == {
            "feedback": None,
            "state": _serialized(before),
            "event": None,
            "skipped": True,
        }
    assert state == before


@pytest.mark.parametrize(
    "attempted_models", [None, [], (), "model-a", ["model-a", ""], ["model-a", 123]]
)
def test_all_failed_without_last_model_preserves_state(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
    attempted_models: object,
) -> None:
    before = deepcopy(state)
    dispatch = {
        **payload,
        "model_id": None,
        "attempted_models": attempted_models,
        "_topic": DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
        "_state": state,
    }
    assert handler.handle(dispatch) == {
        "feedback": None,
        "state": _serialized(before),
        "event": None,
        "skipped": True,
    }
    assert state == before


@pytest.mark.parametrize("state_key", ["_state", "state"])
@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("latency", [300, 300.9, 0, -1, "300", None])
def test_rehydration_latency_and_immutable_state_transition(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
    state_key: str,
    serialized: bool,
    latency: object,
) -> None:
    prior = _serialized(state) if serialized else state
    before = deepcopy(prior)
    result = handler.handle(
        {
            **payload,
            "success": False,
            "latency_ms": latency,
            "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
            state_key: prior,
        }
    )
    expected = {
        **_serialized(before)["model-a:test"],
        "failure_count": 2,
        "total_count": 3,
        "success_rate": 1 / 3,
        "escalation_rate": 1 / 3,
        "avg_latency_ms": 200.0
        if isinstance(latency, int | float) and latency > 0
        else 150.0,
        "last_updated": NOW,
    }
    assert result == {
        "feedback": expected,
        "state": {"model-a:test": expected, "model-a:review": before["model-a:review"]},
        "event": {
            "correlation_id": "corr-1",
            "feedback": expected,
            "source_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        },
        "skipped": False,
    }
    assert prior == before


def test_runtime_state_takes_precedence_over_legacy_state(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> None:
    before = deepcopy(state)
    dispatch = {
        **payload,
        "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
        "_state": {},
        "state": state,
    }
    _assert_update(
        handler.handle(dispatch), source_topic=DELEGATION_CALL_COMPLETED_TOPIC_V1
    )
    assert state == before


@pytest.mark.parametrize(
    "latency",
    [float("nan"), float("inf"), float("-inf"), True, False],
    ids=["nan", "inf", "neg-inf", "true", "false"],
)
def test_unusable_latency_is_ignored_and_never_crashes_the_dispatcher(
    handler: reducer.HandlerDelegationRoutingFeedback,
    payload: dict[str, Any],
    state: dict[str, Any],
    latency: object,
) -> None:
    """A latency that is not a real measurement counts as no latency (OMN-13216).

    The reducer's contract is that a malformed terminal never raises out of the
    dispatcher, because a raise swallows the terminal and splits the request and
    terminal high-water marks. NaN and infinity cannot convert to an int, and a
    bool is not a duration, so each is treated like a missing latency: the event
    still counts, and the running average stays where it was.
    """
    before = deepcopy(state)
    result = handler.handle(
        {
            **payload,
            "success": False,
            "latency_ms": latency,
            "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
            "_state": state,
        }
    )
    assert result["skipped"] is False
    assert result["feedback"]["total_count"] == 3
    assert result["feedback"]["failure_count"] == 2
    assert result["feedback"]["avg_latency_ms"] == 150.0
    assert result["state"]["model-a:test"] == result["feedback"]
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
