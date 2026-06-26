# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13644: context_pack_hash persists onto EVERY delegation terminal.

The context-pack hash is captured ONCE at request acceptance into
``DelegationWorkflowState.context_pack_hash`` and threaded through the single
terminal builder (``HandlerDelegationWorkflow._emit_terminal``) onto the canonical
``ModelDelegationResult``. This proves the hash survives onto BOTH the COMPLETED
and the FAILED/ESCALATED terminals — escalation re-routing or prompt-text loss
between attempts must NOT drop it — and that the projection converter carries it
through to the row mapping. The '' OFF-arm default (no context pack supplied) is
preserved verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
)

_CONTEXT_HASH = (
    "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
_PROMPT_TOKENS = 1234
_COMPLETION_TOKENS = 567


def _make_request(
    correlation_id: UUID, context_pack_hash: str
) -> ModelDelegationRequest:
    # The orchestrator only carries the hash when an actual context pack was
    # injected (``_context_pack_hash_for_event`` gates on context_pack presence).
    # ON arm: supply both the pack text and its hash; OFF arm: neither.
    context_pack = "PROJECT CONTEXT: omni_home registry." if context_pack_hash else ""
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        context_pack=context_pack,
        context_pack_hash=context_pack_hash,
    )


def _make_routing_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-coder"),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="low",
        tier_name="local",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed via tier 'local'.",
    )


def _make_success_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13644",
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
        llm_call_id="chatcmpl-omn13644-failed",
        latency_ms=900,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
        error_message="empty message content from upstream model",
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
    canonical = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    assert len(canonical) == 1, (
        f"expected exactly one canonical terminal, got {events!r}"
    )
    return canonical[0]


def _drive_completed(context_pack_hash: str) -> list[object]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid, context_pack_hash))
    handler.handle_routing_decision(_make_routing_decision(cid))
    handler.handle_inference_response(_make_success_response(cid))
    events = handler.handle_gate_result(_make_gate_result(cid))
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
    return list(events)


def _drive_failed_inference(context_pack_hash: str) -> list[object]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid, context_pack_hash))
    handler.handle_routing_decision(_make_routing_decision(cid))
    events = handler.handle_inference_response(_make_failed_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    return list(events)


@pytest.mark.unit
class TestContextPackHashOnTerminals:
    """The hash captured at acceptance persists onto both terminal shapes."""

    def test_acceptance_pins_hash_into_workflow_state(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid, _CONTEXT_HASH))
        assert handler.workflows[cid].context_pack_hash == _CONTEXT_HASH

    def test_completed_terminal_carries_hash(self) -> None:
        canonical = _single_canonical(_drive_completed(_CONTEXT_HASH))
        assert canonical.quality_passed is True
        assert canonical.context_pack_hash == _CONTEXT_HASH

    def test_failed_terminal_carries_hash(self) -> None:
        # Escalation / prompt-text loss must NOT drop the hash: the FAILED
        # terminal reads the acceptance-pinned value, not the live request.
        canonical = _single_canonical(_drive_failed_inference(_CONTEXT_HASH))
        assert canonical.quality_passed is False
        assert canonical.context_pack_hash == _CONTEXT_HASH

    def test_off_arm_default_preserved_on_both_terminals(self) -> None:
        for events in (_drive_completed(""), _drive_failed_inference("")):
            canonical = _single_canonical(events)
            assert canonical.context_pack_hash == ""


@pytest.mark.unit
class TestContextPackHashProjectionConverter:
    """The projection converter carries the canonical hash into the row event."""

    def _canonical_payload(self, context_pack_hash: str) -> dict[str, object]:
        return {
            "correlation_id": str(uuid4()),
            "task_type": "test",
            "model_used": "qwen3-coder-30b",
            "quality_passed": True,
            "quality_score": 0.9,
            "latency_ms": 1200,
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _COMPLETION_TOKENS,
            "failure_reason": "",
            "escalation_history": (),
            "context_pack_hash": context_pack_hash,
        }

    def test_converter_carries_non_empty_hash(self) -> None:
        payload = _canonical_result_to_task_delegated_payload(
            self._canonical_payload(_CONTEXT_HASH)
        )
        event = ModelTaskDelegatedEvent(**payload)
        assert event.context_pack_hash == _CONTEXT_HASH

    def test_converter_preserves_off_arm_default(self) -> None:
        payload = _canonical_result_to_task_delegated_payload(
            self._canonical_payload("")
        )
        event = ModelTaskDelegatedEvent(**payload)
        assert event.context_pack_hash == ""

    def test_converter_defaults_missing_hash_to_blank(self) -> None:
        raw = self._canonical_payload(_CONTEXT_HASH)
        del raw["context_pack_hash"]
        payload = _canonical_result_to_task_delegated_payload(raw)
        event = ModelTaskDelegatedEvent(**payload)
        assert event.context_pack_hash == ""
