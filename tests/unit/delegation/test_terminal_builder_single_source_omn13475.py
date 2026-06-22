# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13475: one contract-owned builder produces BOTH terminal events.

This is the permanent kill of the OMN-13408 telemetry-zeroing bug *class*.

Before this work the delegation orchestrator built the canonical terminal event
(``delegation-completed.v1`` / ``delegation-failed.v1`` carrying a
``ModelDelegationResult``) AND the backward-compat twin
(``task-delegated.v1`` carrying a ``ModelTaskDelegatedEvent``) in **separate
ad-hoc code paths** at five distinct terminal sites. The two were constructed
from independently-read fields and independently-measured costs, so nothing
structurally guaranteed they agreed. They co-write the same delegation
projection row, so a drifted/zeroed twin clobbered the row's tokens/cost back to
``0`` (OMN-13408). Patching each site to "also carry the tokens" left the
divergence possible at every future edit.

The fix collapses every terminal emission onto ONE builder
(``HandlerDelegationWorkflow._emit_terminal``) that measures cost ONCE and reads
the token counts ONCE, then emits BOTH events from that single source. These
tests assert the *parity invariant* directly: for every terminal path the
canonical ``ModelDelegationResult`` and the compat ``ModelTaskDelegatedEvent``
carry the SAME served tokens and the SAME measured cost. They FAIL on any future
re-introduction of an independent second construction path.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

import omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow as _handler_mod
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_NON_RETRYABLE_ERROR = "empty message content from upstream model"
_PROMPT_TOKENS = 1234
_COMPLETION_TOKENS = 567


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID, tier_name: str
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}-coder"
        ),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="low",
        tier_name=tier_name,
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
    )


def _make_success_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13475",
        latency_ms=1200,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
    )


def _make_failed_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13475-failed",
        latency_ms=900,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
        error_message=_NON_RETRYABLE_ERROR,
    )


def _make_gate_result(correlation_id: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=True,
        quality_score=0.9,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _split(
    events: list[object],
) -> tuple[ModelDelegationResult, ModelTaskDelegatedEvent]:
    """Pull the canonical terminal payload + the compat twin out of one emission.

    Asserts the two events are co-emitted from the SAME terminal builder call:
    exactly one canonical ``ModelDelegationEvent`` (carrying
    ``ModelDelegationResult``) and exactly one ``ModelTaskDelegatedEvent``.
    """
    canonical = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    compat = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
    assert len(canonical) == 1, f"expected one canonical terminal, got {events!r}"
    assert len(compat) == 1, f"expected one compat twin, got {events!r}"
    return canonical[0], compat[0]


def _drive_completed() -> list[object]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    handler.handle_inference_response(_make_success_response(cid))
    events = handler.handle_gate_result(_make_gate_result(cid))
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
    return list(events)


def _drive_failed_inference() -> list[object]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    events = handler.handle_inference_response(_make_failed_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    return list(events)


def _count_constructions(model_name: str) -> int:
    """Count direct ``ModelName(...)`` constructor call sites in the handler."""
    source = Path(_handler_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == model_name
        ):
            count += 1
    return count


@pytest.mark.unit
class TestSingleBuilderStructureOmn13475:
    """The dual ad-hoc construction is gone: each terminal model is built once.

    This is the structural guarantee behind the parity tests below. If anyone
    re-introduces a second independent ``ModelDelegationResult(...)`` or
    ``ModelTaskDelegatedEvent(...)`` construction site, the OMN-13408 divergence
    becomes possible again — and this test fails immediately, at edit time,
    before any projection can be clobbered.
    """

    def test_canonical_terminal_result_built_exactly_once(self) -> None:
        assert _count_constructions("ModelDelegationResult") == 1

    def test_compat_event_built_exactly_once(self) -> None:
        assert _count_constructions("ModelTaskDelegatedEvent") == 1


@pytest.mark.unit
class TestTerminalBuilderSingleSourceOmn13475:
    """Canonical terminal and compat twin agree because ONE builder makes both."""

    def test_completed_path_terminal_and_compat_token_parity(self) -> None:
        canonical, compat = _split(_drive_completed())
        # Single-source invariant: the served tokens on the canonical terminal
        # equal the served tokens on the compat twin (not independently read).
        assert canonical.prompt_tokens == compat.tokens_input == _PROMPT_TOKENS
        assert canonical.completion_tokens == compat.tokens_output == _COMPLETION_TOKENS
        assert compat.tokens_input > 0
        assert compat.tokens_output > 0

    def test_completed_path_terminal_and_compat_cost_parity(self) -> None:
        canonical, compat = _split(_drive_completed())
        # cost_usd on the compat twin is the SAME measured actual cost banked on
        # the canonical terminal's final_attempt_cost — measured exactly once.
        assert compat.cost_usd == canonical.final_attempt_cost
        assert compat.cost_usd == canonical.cumulative_attempt_cost

    def test_failed_path_terminal_and_compat_token_parity(self) -> None:
        canonical, compat = _split(_drive_failed_inference())
        assert canonical.quality_passed is False
        assert compat.quality_gate_passed is False
        assert canonical.prompt_tokens == compat.tokens_input == _PROMPT_TOKENS
        assert canonical.completion_tokens == compat.tokens_output == _COMPLETION_TOKENS
        assert compat.tokens_input > 0
        assert compat.tokens_output > 0

    def test_failed_path_terminal_and_compat_cost_parity(self) -> None:
        canonical, compat = _split(_drive_failed_inference())
        assert compat.cost_usd == canonical.final_attempt_cost
        assert compat.cost_usd == canonical.cumulative_attempt_cost

    def test_both_paths_correlation_and_model_parity(self) -> None:
        for events in (_drive_completed(), _drive_failed_inference()):
            canonical, compat = _split(events)
            assert canonical.correlation_id == compat.correlation_id
            assert canonical.task_type == compat.task_type
            # The twin's delegated_to is the same model the canonical used.
            assert canonical.model_used == compat.delegated_to
