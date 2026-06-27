# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_delegate_skill_orchestrator [OMN-13684].

WS-5 Wave 10, ORCHESTRATOR -> Variant B (bus round-trip). The real
``HandlerDelegateSkill`` is registered on the in-memory event bus via
``LocalRuntimeBusAdapter``; a start command is published on the command topic and
the terminal ``ModelDelegateSkillResponse`` is asserted off the completion topic.

The model router / runtime delegation is mocked at the injected
``ProtocolDelegationDispatchPort`` seam (the ``_MockDispatchPort``) — NEVER a live
LLM, NEVER live Kafka, NEVER a monkeypatched transport. Wave caveat variants
covered: cheap-tier success, escalation (multi-attempt up-tier), task-type
routing, max-tokens threading, and terminal failure (negative control).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_COMMAND_TOPIC = "onex.cmd.omnimarket.delegate-skill.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"


class _MockDispatchPort:
    """Injected model-router boundary. Records the dispatch kwargs and returns a
    pre-configured runtime result dict (no LLM, no broker)."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return dict(self._result)


def _cheap_success() -> dict[str, Any]:
    return {
        "status": "completed",
        "content": "cheap tier answer",
        "model_name": "glm-4.6",
        "delegated_to": "local-glm",
        "quality_gate_passed": True,
        "quality_score": 0.9,
        "compliance_attempts": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.0001,
    }


def _escalated_success() -> dict[str, Any]:
    return {
        "status": "completed",
        "content": "escalated answer",
        "model_name": "claude-opus-4-6",
        "delegated_to": "cloud-claude",
        "quality_gate_passed": True,
        "quality_score": 0.96,
        "compliance_attempts": 3,
        "input_tokens": 200,
        "output_tokens": 120,
        "cost_usd": 0.01,
    }


def _terminal_failure() -> dict[str, Any]:
    return {
        "status": "failed",
        "failure_reason": "quality gate exhausted after retries",
        "quality_gates_failed": ["dod_evidence"],
        "compliance_attempts": 2,
    }


# (case_id, task_type, max_tokens, result_dict, expected)
CASES = [
    pytest.param(
        "code_generation",
        None,
        _cheap_success(),
        {
            "status": "completed",
            "model_name": "glm-4.6",
            "provider": "local-glm",
            "compliance_attempts": 1,
            "quality_gate_passed": True,
        },
        id="cheap-tier-success",
    ),
    pytest.param(
        "complex_reasoning",
        None,
        _escalated_success(),
        {
            "status": "completed",
            "model_name": "claude-opus-4-6",
            "provider": "cloud-claude",
            "compliance_attempts": 3,
            "quality_gate_passed": True,
        },
        id="escalation-up-tier",
    ),
    pytest.param(
        "code_review",
        500,
        _cheap_success(),
        {
            "status": "completed",
            "model_name": "glm-4.6",
            "provider": "local-glm",
            "compliance_attempts": 1,
            "quality_gate_passed": True,
        },
        id="task-type-and-max-tokens-threading",
    ),
    pytest.param(
        # NEGATIVE CONTROL: runtime returns a terminal failure with a failed
        # quality gate. The terminal event must report status=failed with a real
        # error message and the failed-gate list preserved.
        "test",
        None,
        _terminal_failure(),
        {
            "status": "failed",
            "error_contains": "quality gate exhausted",
            "quality_gates_failed": ["dod_evidence"],
        },
        id="terminal-failure-NEGATIVE",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("task_type", "max_tokens", "result_dict", "expected"), CASES)
async def test_delegate_skill_round_trip(
    integration_event_bus: Any,
    task_type: str,
    max_tokens: int | None,
    result_dict: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    await integration_event_bus.start()
    try:
        port = _MockDispatchPort(result_dict)
        handler = HandlerDelegateSkill(dispatch_port=port)
        adapter = LocalRuntimeBusAdapter(
            handler=handler,
            handler_name="delegate-skill",
            input_model_cls=ModelDelegateSkillRequest,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _COMMAND_TOPIC,
            on_message=adapter.on_message,
            group_id="omnimarket-delegate-skill-test",
        )

        correlation_id = uuid4()
        request = ModelDelegateSkillRequest(
            prompt="please do the thing",
            task_type=task_type,
            source="claude-code",
            max_tokens=max_tokens,
            correlation_id=correlation_id,
        )
        await integration_event_bus.publish(
            _COMMAND_TOPIC,
            key=None,
            value=request.model_dump_json().encode("utf-8"),
        )

        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1, "expected exactly one terminal event"
        payload = json.loads(history[0].value)

        # correlation preserved across the round trip
        assert UUID(payload["correlation_id"]) == correlation_id
        assert payload["task_type"] == task_type
        assert payload["status"] == expected["status"]

        # the mock router was invoked exactly once with the threaded params
        assert len(port.calls) == 1
        assert port.calls[0]["task_type"] == task_type
        assert port.calls[0]["max_tokens"] == max_tokens

        if expected["status"] == "failed":
            assert expected["error_contains"] in payload["error_message"]
            assert payload["quality_gates_failed"] == expected["quality_gates_failed"]
        else:
            assert payload["model_name"] == expected["model_name"]
            assert payload["provider"] == expected["provider"]
            assert payload["quality_gate_passed"] == expected["quality_gate_passed"]
            assert (
                payload["metrics"]["compliance_attempts"]
                == expected["compliance_attempts"]
            )
    finally:
        await integration_event_bus.close()
