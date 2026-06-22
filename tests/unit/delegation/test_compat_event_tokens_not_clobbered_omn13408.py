# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408: the delegation compat event carries the served tokens.

Regression coverage for the projection token-clobber gap closed in OMN-13408.

The omnidash delegation projection upserts a single row per ``correlation_id``
from BOTH the canonical typed event (``delegation-failed.v1`` /
``delegation-completed.v1``, which carry real ``prompt_tokens`` /
``completion_tokens``) and the backward-compat ``task-delegated.v1`` event
(``ModelTaskDelegatedEvent``). Before the fix the compat event left
``tokens_input`` / ``tokens_output`` at their model default of ``0`` on BOTH
terminal paths, so whichever event landed last clobbered the row's token
columns back to ``0`` — the dashboard then rendered zero tokens even though the
inference had served thousands.

The fix populates the compat event's ``tokens_input`` / ``tokens_output`` from
the same served token counts the typed event uses:

  * failed terminal path -> ``response.prompt_tokens`` / ``.completion_tokens``
  * completed path (``_build_compat_event``) ->
    ``workflow.inference_prompt_tokens`` / ``.inference_completion_tokens``

These tests drive the success path and a terminal-failed path and assert the
emitted ``ModelTaskDelegatedEvent`` carries the served tokens (>0 when the
inference returned tokens). They FAIL on pre-fix code (compat event tokens == 0)
and PASS after the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
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


def _drive_success_to_terminal() -> ModelTaskDelegatedEvent:
    """Run request->route->infer->gate-pass; return the compat task-delegated.v1."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    handler.handle_inference_response(_make_success_response(cid))
    intents = handler.handle_gate_result(_make_gate_result(cid))
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
    return next(e for e in intents if isinstance(e, ModelTaskDelegatedEvent))


def _drive_failure_to_terminal() -> ModelTaskDelegatedEvent:
    """Run request->route->infer-with-nonretryable-error; return the compat event."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    events = handler.handle_inference_response(_make_failed_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    # The terminal-failed path returns [delegation-failed.v1, compat task-delegated.v1].
    compat = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
    assert len(compat) == 1, f"expected exactly one compat event, got {events!r}"
    return compat[0]


@pytest.mark.unit
class TestCompatEventTokensNotClobberedOmn13408:
    """The compat task-delegated.v1 event carries the served tokens, not 0."""

    def test_completed_path_compat_event_carries_served_tokens(self) -> None:
        """``_build_compat_event`` (completed path) must set tokens_input/output
        to the served inference tokens — NOT leave them at the 0 default that
        would clobber the projection row."""
        compat = _drive_success_to_terminal()

        # The bug-fix invariant: served tokens are carried, strictly positive.
        assert compat.tokens_input == _PROMPT_TOKENS
        assert compat.tokens_output == _COMPLETION_TOKENS
        assert compat.tokens_input > 0
        assert compat.tokens_output > 0

    def test_failed_path_compat_event_carries_served_tokens(self) -> None:
        """The terminal-failed compat event must carry the tokens the upstream
        model served before failing — the canonical delegation-failed.v1 event
        co-writes the same projection row with those same tokens, so the compat
        event must match rather than zero them."""
        compat = _drive_failure_to_terminal()

        assert compat.quality_gate_passed is False
        assert compat.tokens_input == _PROMPT_TOKENS
        assert compat.tokens_output == _COMPLETION_TOKENS
        assert compat.tokens_input > 0
        assert compat.tokens_output > 0

    def test_both_terminal_paths_carry_nonzero_tokens(self) -> None:
        """Cross-check: neither terminal path emits a tokens-clobbering 0 when
        the inference served tokens. This is the exact regression: pre-fix BOTH
        paths emitted tokens_input == tokens_output == 0."""
        completed = _drive_success_to_terminal()
        failed = _drive_failure_to_terminal()

        for event in (completed, failed):
            assert event.tokens_input == _PROMPT_TOKENS
            assert event.tokens_output == _COMPLETION_TOKENS
            assert (event.tokens_input, event.tokens_output) != (0, 0)
