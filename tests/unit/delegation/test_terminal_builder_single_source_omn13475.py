# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13475 / OMN-13629: one contract-owned builder produces ONE terminal event.

This is the permanent kill of the OMN-13408 telemetry-zeroing bug *class*.

History: the delegation orchestrator used to build the canonical terminal event
(``delegation-completed.v1`` / ``delegation-failed.v1`` carrying a
``ModelDelegationResult``) AND a backward-compat twin (``task-delegated.v1``
carrying a ``ModelTaskDelegatedEvent``) at five distinct terminal sites. The two
were constructed from independently-read fields and independently-measured costs,
so nothing structurally guaranteed they agreed. They co-write the same delegation
projection row, so a drifted/zeroed twin clobbered the row's tokens/cost back to
``0`` (OMN-13408). OMN-13475 collapsed every emission onto ONE builder
(``HandlerDelegationWorkflow._emit_terminal``) that measured cost ONCE; OMN-13629
(WS-F Phase 1) then DELETED the compat twin outright, collapsing the terminal to
a SINGLE canonical event. With exactly one writer the divergence is not merely
constrained — it is structurally impossible.

These tests assert the post-13629 invariant directly: every terminal path emits
EXACTLY ONE canonical ``ModelDelegationResult`` terminal and ZERO compat events.
They FAIL on any re-introduction of a second terminal construction path.
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


def _single_canonical(events: list[object]) -> ModelDelegationResult:
    """Pull THE single canonical terminal payload out of one emission.

    OMN-13629: asserts the terminal is a SINGLE canonical
    ``ModelDelegationEvent`` (carrying ``ModelDelegationResult``) and that NO
    compat ``ModelTaskDelegatedEvent`` twin is co-emitted — the legacy co-writer
    that drove the OMN-13408 divergence is gone.
    """
    canonical = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    compat = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
    assert len(canonical) == 1, (
        f"expected exactly one canonical terminal, got {events!r}"
    )
    assert compat == [], f"expected ZERO compat twins (OMN-13629), got {compat!r}"
    return canonical[0]


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
class TestSingleBuilderStructureOmn13629:
    """The terminal collapses to ONE canonical construction; the compat twin is gone.

    If anyone re-introduces a second ``ModelDelegationResult(...)`` or a
    ``ModelTaskDelegatedEvent(...)`` construction in the workflow handler, the
    OMN-13408 divergence becomes possible again — and this test fails immediately,
    at edit time, before any projection can be clobbered.
    """

    def test_canonical_terminal_result_built_exactly_once(self) -> None:
        assert _count_constructions("ModelDelegationResult") == 1

    def test_compat_event_no_longer_constructed(self) -> None:
        # OMN-13629: the legacy ModelTaskDelegatedEvent co-writer was deleted.
        assert _count_constructions("ModelTaskDelegatedEvent") == 0


@pytest.mark.unit
class TestSingleTerminalEmissionOmn13629:
    """Every terminal path emits EXACTLY ONE canonical terminal, ZERO compat."""

    def test_completed_path_emits_single_canonical_terminal(self) -> None:
        canonical = _single_canonical(_drive_completed())
        assert canonical.quality_passed is True
        assert canonical.prompt_tokens == _PROMPT_TOKENS
        assert canonical.completion_tokens == _COMPLETION_TOKENS

    def test_failed_path_emits_single_canonical_terminal(self) -> None:
        canonical = _single_canonical(_drive_failed_inference())
        assert canonical.quality_passed is False
        assert canonical.prompt_tokens == _PROMPT_TOKENS
        assert canonical.completion_tokens == _COMPLETION_TOKENS

    def test_both_paths_carry_correlation_and_model(self) -> None:
        for events in (_drive_completed(), _drive_failed_inference()):
            canonical = _single_canonical(events)
            assert canonical.task_type == "test"
            assert canonical.model_used == "qwen3-coder-30b"
