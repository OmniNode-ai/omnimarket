# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity proof for the node_delegation_routing_feedback_reducer def-B flip.

``handle`` took a raw ``object`` (envelope unwrapping, topic and state markers
inside the core) and returned a dict the runtime drops. It now takes a typed
terminal payload and returns the event the contract publishes. The preserved
business logic is ``accumulate``; these tests pin that ``handle`` drives it
exactly as the old topic-keyed normalization did, over the adequacy corpus.

Imports of the new models are lazy so that, at the pre-flip base ref, every id
fails on its own assertion rather than on a collection error.
"""

from __future__ import annotations

import inspect
import math
import typing
from typing import Any

import pytest
from pydantic import BaseModel

pytestmark = pytest.mark.unit

_FROZEN_NOW = "2026-10-05T00:00:00+00:00"


def _handler_module() -> Any:
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers import (
        handler_delegation_routing_feedback as module,
    )

    return module


def _assert_single_typed_payload_handle() -> None:
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
        HandlerDelegationRoutingFeedback,
    )

    parameters = [
        p
        for name, p in inspect.signature(
            HandlerDelegationRoutingFeedback.handle
        ).parameters.items()
        if name != "self"
    ]
    assert len(parameters) == 1
    annotation = typing.get_type_hints(HandlerDelegationRoutingFeedback.handle)[
        parameters[0].name
    ]
    assert isinstance(annotation, type)
    assert issubclass(annotation, BaseModel), (
        "handle must take a BaseModel payload, not a raw object"
    )


def corpus() -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """(raw terminal payload, expected normalized event fields or None for no-op)."""
    completed = {
        "correlation_id": "c1",
        "request_id": "r1",
        "task_type": "codegen",
        "model_id": "qwen3-coder-30b",
        "success": True,
        "latency_ms": 250,
        "tokens_in": 100,
    }
    expected_completed = {
        "event_type": "delegation-call-completed",
        "correlation_id": "c1",
        "request_id": "r1",
        "task_type": "codegen",
        "model_id": "qwen3-coder-30b",
        "success": True,
        "is_escalation": False,
        "latency_ms": 250,
    }
    escalation = {
        "correlation_id": "c2",
        "request_id": "r2",
        "task_type": "codegen",
        "model_id": "ds-v4-flash",
        "attempt_number": 1,
        "escalation_reason": "timed out",
        "next_model_id": "claude",
    }
    failed = {
        "correlation_id": "c3",
        "request_id": "r3",
        "task_type": "codegen",
        "attempted_models": ["ds-v4-flash", "qwen3-coder-30b", "claude"],
    }
    return [
        (completed, expected_completed),
        (
            {**completed, "success": False},
            {**expected_completed, "success": False},
        ),
        (
            {**completed, "latency_ms": math.nan},
            {**expected_completed, "latency_ms": 0},
        ),
        (
            {**completed, "latency_ms": True},
            {**expected_completed, "latency_ms": 0},
        ),
        ({**completed, "latency_ms": 0}, {**expected_completed, "latency_ms": 0}),
        (
            escalation,
            {
                "event_type": "delegation-escalation-triggered",
                "correlation_id": "c2",
                "request_id": "r2",
                "task_type": "codegen",
                "model_id": "ds-v4-flash",
                "success": False,
                "is_escalation": True,
                "latency_ms": 0,
            },
        ),
        (
            failed,
            {
                "event_type": "delegation-all-tiers-failed",
                "correlation_id": "c3",
                "request_id": "r3",
                "task_type": "codegen",
                "model_id": "claude",
                "success": False,
                "is_escalation": False,
                "latency_ms": 0,
            },
        ),
        (
            {**failed, "model_id": "qwen3-coder-30b"},
            {
                "event_type": "delegation-all-tiers-failed",
                "correlation_id": "c3",
                "request_id": "r3",
                "task_type": "codegen",
                "model_id": "qwen3-coder-30b",
                "success": False,
                "is_escalation": False,
                "latency_ms": 0,
            },
        ),
        (
            {**completed, "event_type": "delegation-escalation-triggered"},
            {
                **expected_completed,
                "event_type": "delegation-escalation-triggered",
                "success": False,
                "is_escalation": True,
                "latency_ms": 0,
            },
        ),
        ({**completed, "task_type": ""}, None),
        ({**completed, "model_id": ""}, None),
        ({**failed, "attempted_models": []}, None),
        ({}, None),
    ]


def test_handle_is_single_payload_adaptable() -> None:
    _assert_single_typed_payload_handle()


def test_multipositional_or_object_handle_is_the_red() -> None:
    _assert_single_typed_payload_handle()
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
        HandlerDelegationRoutingFeedback,
    )

    source = inspect.getsource(inspect.getmodule(HandlerDelegationRoutingFeedback))
    assert "ModelEventEnvelope" not in source
    assert "_unwrap_envelope" not in source


@pytest.mark.parametrize(("index"), range(13))
def test_handle_matches_preserved_accumulate_over_corpus(
    index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_single_typed_payload_handle()
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
        HandlerDelegationRoutingFeedback,
    )
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_feedback_event import (
        ModelDelegationFeedbackEvent,
    )
    from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_terminal_payload import (
        ModelDelegationTerminalPayload,
    )

    module = _handler_module()
    monkeypatch.setattr(module, "_now_iso", lambda: _FROZEN_NOW)
    raw, expected = corpus()[index]
    handler = HandlerDelegationRoutingFeedback()
    produced = handler.handle(ModelDelegationTerminalPayload(**raw))

    if expected is None:
        assert produced is None
        return
    event = ModelDelegationFeedbackEvent(**expected)
    feedback, _ = handler.accumulate(event, {})
    assert produced is not None
    assert produced.feedback == feedback
    assert produced.correlation_id == event.correlation_id
