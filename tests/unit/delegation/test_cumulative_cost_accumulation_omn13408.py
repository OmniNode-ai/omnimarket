# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408: cumulative_attempt_cost and cumulative_input_tokens must be non-zero
for metered delegations.

Regression test for the bug where _escalation_metadata always returned
hardcoded zeros for cumulative_attempt_cost, cumulative_input_tokens,
cumulative_output_tokens, and final_attempt_cost — meaning every terminal
ModelDelegationResult (whether success, failure, or escalation-exhausted) had
these fields stuck at 0 regardless of actual per-attempt inference cost.

Reproduction:
    A cheap_cloud (metered) delegation with 769 prompt tokens and 200 completion
    tokens at rate_per_1k_usd=0.002 must yield:
      - cumulative_attempt_cost  > 0  (was: 0.0)
      - cumulative_input_tokens  > 0  (was: 0  — prompt_tokens=769 non-zero)
      - cumulative_output_tokens > 0  (was: 0  — completion_tokens=200 non-zero)
      - final_attempt_cost       > 0  (was: 0.0)

These fields feed the projection cost_usd column. Zero here = zero projected cost
even when real inference tokens were metered.

Dogfood cid: 54649717 (live delegation with GLM/cheap_cloud, cost_usd=0.0 observed
in /v1/projection/delegation endpoint).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.pricing import recompute_actual_cost_and_savings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Dogfood repro: 769 input tokens + 200 output tokens on cheap_cloud (GLM).
_PROMPT_TOKENS = 769
_COMPLETION_TOKENS = 200
_TIER_NAME = "cheap_cloud"


def _make_request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Explain the delegation cost accumulation bug.",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    cid: UUID, tier_name: str = _TIER_NAME
) -> ModelRoutingDecision:
    from uuid import NAMESPACE_DNS, uuid5

    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="glm-4-flash",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for GLM endpoint"
        cost_tier="low",
        tier_name=tier_name,
        max_context_tokens=128000,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
    )


def _make_inference_response(
    cid: UUID,
    prompt_tokens: int = _PROMPT_TOKENS,
    completion_tokens: int = _COMPLETION_TOKENS,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="The delegation cost accumulation bug is caused by hardcoded zeros.",
        model_used="glm-4-flash",
        llm_call_id="chatcmpl-omn13408",
        latency_ms=850,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _make_gate_pass(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=True,
        quality_score=0.88,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _make_gate_fail(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=False,
        quality_score=0.2,
        failure_reasons=("score_below_required_bar",),
        fallback_recommended=True,
    )


# ---------------------------------------------------------------------------
# Tests — OMN-13408 dogfood repro
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCumulativeCostAccumulationOmn13408:
    """cumulative_attempt_cost and cumulative_*_tokens must be non-zero
    for metered delegations (dogfood cid 54649717)."""

    def test_success_path_cumulative_cost_nonzero_for_metered_tier(self) -> None:
        """Happy path: completed delegation on cheap_cloud tier must carry
        cumulative_attempt_cost > 0 and cumulative_input_tokens > 0."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(cid, _PROMPT_TOKENS, _COMPLETION_TOKENS)
        )
        events = handler.handle_gate_result(_make_gate_pass(cid))
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED

        delegation_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(delegation_events) == 1
        result = delegation_events[0].payload

        # OMN-13408 invariants: cumulative costs must NOT be stuck at 0
        assert result.cumulative_attempt_cost > 0.0, (
            f"cumulative_attempt_cost={result.cumulative_attempt_cost!r} — "
            f"expected > 0 for metered cheap_cloud tier "
            f"({_PROMPT_TOKENS}+{_COMPLETION_TOKENS} tokens)"
        )
        assert result.cumulative_input_tokens > 0, (
            f"cumulative_input_tokens={result.cumulative_input_tokens!r} — "
            f"expected > 0 (prompt_tokens={_PROMPT_TOKENS})"
        )
        assert result.cumulative_output_tokens > 0, (
            f"cumulative_output_tokens={result.cumulative_output_tokens!r} — "
            f"expected > 0 (completion_tokens={_COMPLETION_TOKENS})"
        )
        assert result.final_attempt_cost > 0.0, (
            f"final_attempt_cost={result.final_attempt_cost!r} — "
            f"expected > 0 for metered tier"
        )

        # Authoritative cross-check against the pricing module
        expected_cost = recompute_actual_cost_and_savings(
            tier_name=_TIER_NAME,
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            premium_counterfactual=None,
        )
        assert expected_cost.cash_cost_usd > 0.0
        assert result.cumulative_attempt_cost == pytest.approx(
            expected_cost.cash_cost_usd
        )
        assert result.final_attempt_cost == pytest.approx(expected_cost.cash_cost_usd)
        assert result.cumulative_input_tokens == _PROMPT_TOKENS
        assert result.cumulative_output_tokens == _COMPLETION_TOKENS

    def test_failed_path_cumulative_cost_nonzero_for_metered_tier(self) -> None:
        """Failed delegation (quality gate fail, no escalation) on cheap_cloud
        must still carry non-zero cumulative_attempt_cost — the inference tokens
        were consumed even though quality didn't pass."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(cid, _PROMPT_TOKENS, _COMPLETION_TOKENS)
        )
        # Force terminal failure by exceeding max_escalation_attempts=0
        events = handler.handle_gate_result(
            _make_gate_fail(cid), max_escalation_attempts=0
        )
        assert handler.workflows[cid].state == EnumDelegationState.FAILED

        delegation_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(delegation_events) == 1
        result = delegation_events[0].payload

        # OMN-13408 invariants on the failed terminal path
        assert result.cumulative_attempt_cost > 0.0, (
            f"cumulative_attempt_cost={result.cumulative_attempt_cost!r} on FAILED path — "
            f"tokens were consumed, cost must be non-zero"
        )
        assert result.cumulative_input_tokens > 0
        assert result.cumulative_output_tokens > 0
        assert result.final_attempt_cost > 0.0

    def test_free_local_tier_cumulative_cost_is_zero_by_cost_model(self) -> None:
        """free_local tier: cumulative_attempt_cost is legitimately 0.0 —
        proven by the typed cost model (free_local), not a hardcoded zero."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
        handler.handle_inference_response(
            _make_inference_response(cid, _PROMPT_TOKENS, _COMPLETION_TOKENS)
        )
        events = handler.handle_gate_result(_make_gate_pass(cid))
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED

        delegation_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(delegation_events) == 1
        result = delegation_events[0].payload

        # free_local is legitimately 0 — but token counts must still be populated
        assert result.cumulative_attempt_cost == pytest.approx(0.0)
        assert result.final_attempt_cost == pytest.approx(0.0)
        # Tokens must be correct even when cost is zero
        assert result.cumulative_input_tokens == _PROMPT_TOKENS
        assert result.cumulative_output_tokens == _COMPLETION_TOKENS

    def test_metered_and_free_local_diverge_on_cumulative_cost(self) -> None:
        """Cross-check: same token counts, different tiers — metered yields
        non-zero cumulative cost, free_local yields zero. The fix is
        tier-sensitive, not a constant."""
        prompt, completion = 769, 200

        handler_m = HandlerDelegationWorkflow()
        cid_m = uuid4()
        handler_m.handle_delegation_request(_make_request(cid_m))
        handler_m.handle_routing_decision(_make_routing_decision(cid_m, "cheap_cloud"))
        handler_m.handle_inference_response(
            _make_inference_response(cid_m, prompt, completion)
        )
        events_m = handler_m.handle_gate_result(_make_gate_pass(cid_m))
        result_m = next(
            e.payload for e in events_m if isinstance(e, ModelDelegationEvent)
        )

        handler_f = HandlerDelegationWorkflow()
        cid_f = uuid4()
        handler_f.handle_delegation_request(_make_request(cid_f))
        handler_f.handle_routing_decision(_make_routing_decision(cid_f, "local"))
        handler_f.handle_inference_response(
            _make_inference_response(cid_f, prompt, completion)
        )
        events_f = handler_f.handle_gate_result(_make_gate_pass(cid_f))
        result_f = next(
            e.payload for e in events_f if isinstance(e, ModelDelegationEvent)
        )

        assert result_m.cumulative_attempt_cost > result_f.cumulative_attempt_cost
        assert result_f.cumulative_attempt_cost == pytest.approx(0.0)
        # Token accumulation is identical regardless of tier
        assert result_m.cumulative_input_tokens == prompt
        assert result_f.cumulative_input_tokens == prompt
