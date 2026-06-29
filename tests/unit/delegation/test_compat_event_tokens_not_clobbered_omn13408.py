# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408 / OMN-13629: the canonical delegation terminal carries served tokens.

Regression coverage for the projection token-clobber gap closed in OMN-13408 and
permanently eliminated in OMN-13629.

History: the delegation projection used to upsert a single row per
``correlation_id`` from BOTH the canonical typed event and a backward-compat
``task-delegated.v1`` twin. Before OMN-13408 the compat event left its token
columns at the model default of ``0``, so whichever event landed last clobbered
the row's tokens back to ``0``. OMN-13629 deleted the compat twin outright: the
canonical ``ModelDelegationResult`` (``delegation-completed.v1`` /
``delegation-failed.v1``) is now the SINGLE writer, so a clobbering twin can no
longer exist.

These tests drive the success and terminal-failed paths and assert the canonical
``ModelDelegationResult`` carries the served tokens (>0 when the inference
returned tokens) — the single source the projection row now reads.
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

# A non-retryable inference error marker forces the terminal FAILED path (no
# escalation), so the compat event is emitted alongside delegation-failed.v1.
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
        llm_call_id="chatcmpl-omn13408",
        latency_ms=1200,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
    )


def _make_failed_response(correlation_id: UUID) -> ModelInferenceResponseData:
    """A failed inference that STILL served tokens before the failure was seen.

    The upstream model billed/served ``prompt_tokens`` + ``completion_tokens``
    yet returned unusable content (``error_message`` set). The projection row
    therefore has real tokens on the canonical ``delegation-failed.v1`` event —
    the compat event must not zero them.
    """
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13408-failed",
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


def _drive_success_to_terminal() -> ModelDelegationResult:
    """Run request->route->infer->gate-pass; return the canonical terminal result."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    handler.handle_inference_response(_make_success_response(cid))
    intents = handler.handle_gate_result(_make_gate_result(cid))
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
    return next(
        e.payload
        for e in intents
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    )


def _drive_failure_to_terminal() -> ModelDelegationResult:
    """Run request->route->infer-with-nonretryable-error; return the terminal."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    events = handler.handle_inference_response(_make_failed_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    # OMN-13629: the terminal-failed path returns a single canonical event.
    terminals = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    assert len(terminals) == 1, (
        f"expected exactly one canonical terminal, got {events!r}"
    )
    return terminals[0]


@pytest.mark.unit
class TestCompatEventTokensNotClobberedOmn13408:
    """The canonical delegation terminal carries the served tokens, not 0."""

    def test_completed_path_terminal_carries_served_tokens(self) -> None:
        """The completed-path canonical terminal carries the served inference
        tokens — the single source the projection row reads."""
        terminal = _drive_success_to_terminal()

        assert terminal.prompt_tokens == _PROMPT_TOKENS
        assert terminal.completion_tokens == _COMPLETION_TOKENS
        assert terminal.prompt_tokens > 0
        assert terminal.completion_tokens > 0

    def test_failed_path_terminal_carries_served_tokens(self) -> None:
        """The terminal-failed canonical event must carry the tokens the upstream
        model served before failing — the single source for the projection row."""
        terminal = _drive_failure_to_terminal()

        assert terminal.quality_passed is False
        assert terminal.prompt_tokens == _PROMPT_TOKENS
        assert terminal.completion_tokens == _COMPLETION_TOKENS
        assert terminal.prompt_tokens > 0
        assert terminal.completion_tokens > 0

    def test_both_terminal_paths_carry_nonzero_tokens(self) -> None:
        """Cross-check: neither terminal path zeroes tokens when the inference
        served tokens."""
        completed = _drive_success_to_terminal()
        failed = _drive_failure_to_terminal()

        for terminal in (completed, failed):
            assert terminal.prompt_tokens == _PROMPT_TOKENS
            assert terminal.completion_tokens == _COMPLETION_TOKENS
            assert (terminal.prompt_tokens, terminal.completion_tokens) != (0, 0)
