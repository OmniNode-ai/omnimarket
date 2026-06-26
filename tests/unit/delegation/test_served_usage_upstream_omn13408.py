# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408 (fix attempt 1): served cloud usage must survive the truncation raise.

PRIMARY drop this guards: the cloud inference EFFECT discarded the provider's
served usage when the response truncated. ``HandlerInferenceIntent._call_llm``
raised ``finish_reason=length`` BEFORE parsing the ``usage`` block, and the
error-path ``ModelInferenceResponseData`` omitted the token fields — so they
defaulted to 0/0/0. The merged carry-fix (tokens_input/output on the compat
event + cumulative accumulator in ``_escalation_metadata``) is correct but read
from that zero source, so the canonical ``delegation-failed.v1`` terminal still
recorded 0 tokens / $0 cost on the escalated-and-failed path.

Live EFFECT proof on the dev runtime (omninode-runtime-effects on .201): the
exact failing CID badf851a payload (code_generation, max_tokens=512,
gemini-2.5-flash) returned HTTP 200, finish_reason=length, content_len=51, and a
real OpenAI-shaped usage block ``{"prompt_tokens":22,"completion_tokens":19,
"total_tokens":529}`` — the provider DID meter the call (529 total includes ~488
reasoning tokens because gemini-2.5 is a thinking model). The fix parses usage
before the raise and threads it onto the error-path response via a typed
``InferenceUsageError``.

These tests assert the served tokens flow EFFECT -> orchestrator -> the canonical
``ModelDelegationResult`` (delegation-failed.v1), NOT only the compat twin.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceResponseData

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _MAX_INFERENCE_ESCALATION_ATTEMPTS,
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
    InferenceUsageError,
)
from omnimarket.pricing import recompute_actual_cost_and_savings

# Live CID badf851a usage on the truncated gemini-2.5-flash response.
_PROMPT_TOKENS = 22
_COMPLETION_TOKENS = 19
_TOTAL_TOKENS = 529  # reasoning model: total > prompt + completion
_TIER_NAME = "cheap_cloud"  # metered tier the cost model prices > 0

_TRUNCATED_CLOUD_RESPONSE = {
    "id": "chatcmpl-badf851a",
    "choices": [
        {
            "finish_reason": "length",
            "message": {"content": "def fib(n):\n    if n < 2:\n        return n\n  "},
        }
    ],
    "usage": {
        "prompt_tokens": _PROMPT_TOKENS,
        "completion_tokens": _COMPLETION_TOKENS,
        "total_tokens": _TOTAL_TOKENS,
    },
}


def _make_intent_response_via_effect(cid: UUID) -> ModelInferenceResponseData:
    """Run the real inference EFFECT against a mocked truncated cloud response."""
    from omnibase_core.models.delegation.wire import ModelInferenceIntent

    handler = HandlerInferenceIntent()
    intent = ModelInferenceIntent(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        model="gemini-2.5-flash",
        system_prompt="You are a code generation assistant.",
        prompt="Write a Fibonacci function.",
        max_tokens=512,
        temperature=0.3,
        timeout_seconds=30.0,
        correlation_id=cid,
    )

    mock_response = MagicMock()
    mock_response.json.return_value = _TRUNCATED_CLOUD_RESPONSE
    mock_response.raise_for_status.return_value = None

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        return handler.handle(intent)


def _make_request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write a Fibonacci function.",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=cid,
        max_tokens=512,
        emitted_at=datetime.now(UTC),
    )


def _make_metered_routing_decision(cid: UUID) -> ModelRoutingDecision:
    """A routing decision on the metered ``cheap_cloud`` tier.

    The ceiling tier (``claude`` -> gemini free-tier key) is genuinely
    free_local-class ($0 cost), so a terminal on the ceiling would correctly
    price to $0 even with real tokens. To prove the served usage produces a
    non-zero PRICED cost, the failing tier here is the metered ``cheap_cloud``
    tier (rate_per_1k_usd=0.002). The test forces escalation exhaustion so this
    is the terminal-failed tier rather than a re-route.
    """
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="glm-4-flash",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/cheap_cloud"),
        endpoint_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",  # onex-allow-internal-ip OMN-13408 reason="metered tier endpoint in served-usage regression test"
        cost_tier="low",
        tier_name=_TIER_NAME,
        max_context_tokens=128000,
        max_tokens=65536,
        system_prompt="You are a code generation assistant.",
        rationale="Task 'test' routed via tier 'cheap_cloud'.",
    )


@pytest.mark.unit
class TestServedUsageUpstreamOmn13408:
    """The served usage must survive the truncation raise and reach the canonical
    terminal event."""

    def test_effect_carries_served_usage_on_truncation(self) -> None:
        """PRIMARY: the inference EFFECT error-path response carries the served
        usage that the provider reported even though finish_reason=length."""
        cid = uuid4()
        response = _make_intent_response_via_effect(cid)

        assert response.content == ""
        assert "finish_reason=length" in response.error_message
        # The served usage is preserved, not dropped to 0.
        assert response.prompt_tokens == _PROMPT_TOKENS
        assert response.completion_tokens == _COMPLETION_TOKENS
        assert response.total_tokens == _TOTAL_TOKENS

    def test_typed_usage_exception_carries_usage(self) -> None:
        """The truncation raise is the typed InferenceUsageError carrying usage."""
        from omnibase_core.models.delegation.wire import ModelInferenceIntent

        handler = HandlerInferenceIntent()
        intent = ModelInferenceIntent(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model="gemini-2.5-flash",
            system_prompt="s",
            prompt="p",
            max_tokens=512,
            correlation_id=uuid4(),
        )
        mock_response = MagicMock()
        mock_response.json.return_value = _TRUNCATED_CLOUD_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            with pytest.raises(InferenceUsageError) as exc_info:
                handler._call_llm(intent, "call-id")

        assert exc_info.value.prompt_tokens == _PROMPT_TOKENS
        assert exc_info.value.completion_tokens == _COMPLETION_TOKENS
        assert exc_info.value.total_tokens == _TOTAL_TOKENS

    def test_canonical_failed_terminal_carries_served_tokens_and_cost(self) -> None:
        """END-TO-END escalated-failed: the canonical delegation-failed.v1 event
        (ModelDelegationResult), NOT just the compat twin, must carry the real
        served tokens AND a non-zero priced cost on the truncated cloud path."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(_make_metered_routing_decision(cid))

        # Force escalation exhaustion so the truncation error terminates on the
        # CURRENT (metered cheap_cloud) tier rather than re-routing upward. This
        # mirrors the live escalated-and-failed CID badf851a, which reached the
        # terminal delegation-failed.v1 after escalation_count=2.
        handler.workflows[cid].escalation_count = _MAX_INFERENCE_ESCALATION_ATTEMPTS

        # The inference EFFECT produced the truncated-but-usage-bearing response.
        response = _make_intent_response_via_effect(cid)
        assert response.prompt_tokens == _PROMPT_TOKENS  # precondition

        # Feed it into the orchestrator → terminal FAILED on the metered tier.
        terminal_events = handler.handle_inference_response(response)
        assert handler.workflows[cid].state == EnumDelegationState.FAILED

        # CANONICAL terminal event (delegation-failed.v1).
        delegation_events = [
            e for e in terminal_events if isinstance(e, ModelDelegationEvent)
        ]
        assert len(delegation_events) == 1
        canonical = delegation_events[0].payload
        assert isinstance(canonical, ModelDelegationResult)
        assert delegation_events[0].topic.endswith("delegation-failed.v1")

        # The canonical event carries the served tokens (was 0/0 before the fix).
        # total_tokens is reconciled to prompt + completion at the wire boundary
        # (the reasoning-token total is not separately modeled downstream).
        assert canonical.prompt_tokens == _PROMPT_TOKENS
        assert canonical.completion_tokens == _COMPLETION_TOKENS
        assert canonical.total_tokens == _PROMPT_TOKENS + _COMPLETION_TOKENS
        assert "finish_reason=length" in canonical.failure_reason

        # The cumulative accumulator + final-attempt cost are now non-zero (priced
        # through the metered tier cost model), not the prior hardcoded 0.
        assert canonical.cumulative_input_tokens == _PROMPT_TOKENS
        assert canonical.cumulative_output_tokens == _COMPLETION_TOKENS
        assert canonical.final_attempt_cost > 0.0
        assert canonical.cumulative_attempt_cost > 0.0

        expected_cost = recompute_actual_cost_and_savings(
            tier_name=_TIER_NAME,
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            premium_counterfactual=None,
        )
        assert expected_cost.cash_cost_usd > 0.0
        assert canonical.final_attempt_cost == pytest.approx(
            expected_cost.cash_cost_usd
        )

        # OMN-13629: there is NO compat twin — the canonical terminal above is the
        # single source the projection row reads.
        compat_events = [
            e for e in terminal_events if isinstance(e, ModelTaskDelegatedEvent)
        ]
        assert compat_events == []
