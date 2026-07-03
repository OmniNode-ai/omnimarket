# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13396: the delegate-skill-terminal path emits MEASURED cost_usd.

Regression coverage for the live-honesty gap closed in OMN-13396. The delegation
workflow handler previously constructed every terminal event with a hardcoded
``cost_usd=0.0`` (and ``candidate_cost_usd=0.0`` on the baseline intent), so the
live SEA/delegation chain persisted a zero actual cost and the savings number was
``counterfactual - 0`` — the full counterfactual, overstated by the serving
tier's real (non-zero, for metered) cost.

The fix prices the served tokens through the SAME typed tier cost model
(``ModelTierCost`` / ``EnumTierCostType``, OMN-13234) the projection's recompute
uses, resolved by serving-tier name from the canonical routing registry:

  * ``free_local`` tier  -> measured cost 0.0 (local GPU, no marginal API cost).
  * ``metered`` tier     -> measured tokens x tier rate (non-zero).

These tests drive the success path (compat task-delegated.v1 event + baseline
intent) on a metered tier and a free_local tier and assert the emitted cost.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_baseline_intent import (
    ModelBaselineIntent,
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
    ModelActualCostProjection,
    ModelTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
    _measure_actual_cost,
)
from omnimarket.pricing import recompute_actual_cost_and_savings


def _measure_canonical(canonical: ModelDelegationResult) -> ModelActualCostProjection:
    """The projection's actual-cost measurement from the canonical terminal."""
    converted = _canonical_result_to_task_delegated_payload(
        canonical.model_dump(mode="json")
    )
    return _measure_actual_cost(ModelTaskDelegatedEvent(**converted))


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    from datetime import UTC, datetime

    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID, tier_name: str
) -> ModelRoutingDecision:
    from uuid import NAMESPACE_DNS, uuid5

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


def _make_inference_response(
    correlation_id: UUID,
    prompt_tokens: int,
    completion_tokens: int,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn13396",
        latency_ms=1200,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _make_gate_result(correlation_id: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=True,
        quality_score=0.9,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _drive_success_to_terminal(
    *,
    tier_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[ModelDelegationResult, ModelBaselineIntent]:
    """Run request->route->infer->gate-pass; return (canonical terminal, baseline)."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name=tier_name))
    handler.handle_inference_response(
        _make_inference_response(
            cid, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    )
    intents = handler.handle_gate_result(_make_gate_result(cid))
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED

    canonical = next(
        e.payload
        for e in intents
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    )
    baseline = next(e for e in intents if isinstance(e, ModelBaselineIntent))
    return canonical, baseline


@pytest.mark.unit
class TestTerminalPathCostUsdOmn13396:
    """The terminal path emits measured cost, not a hardcoded 0.0."""

    def test_metered_tier_emits_nonzero_measured_cost(self) -> None:
        """A metered (cheap_cloud) route emits a measured final_attempt_cost ==
        rate x tokens on the canonical terminal (NOT a hardcoded 0.0)."""
        prompt_tokens, completion_tokens = 1000, 500
        canonical, baseline = _drive_success_to_terminal(
            tier_name="cheap_cloud",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        # Authoritative expectation: the SAME computation the orchestrator uses.
        expected = recompute_actual_cost_and_savings(
            tier_name="cheap_cloud",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            premium_counterfactual=None,
        )

        # The measured metered cost is strictly positive — the bug-fix invariant.
        assert expected.cash_cost_usd > 0.0
        assert canonical.final_attempt_cost == pytest.approx(expected.cash_cost_usd)
        assert canonical.final_attempt_cost > 0.0
        assert canonical.cumulative_attempt_cost == pytest.approx(
            expected.cash_cost_usd
        )

        # The baseline intent's candidate (delegated tier) cost is the measured
        # metered cost, not a hardcoded 0.0.
        assert baseline.candidate_cost_usd == pytest.approx(expected.cash_cost_usd)
        assert baseline.candidate_cost_usd > 0.0

    def test_free_local_tier_emits_zero_measured_cost(self) -> None:
        """A free_local (local) route legitimately measures cost == 0.0 — proven by
        the typed cost model, not a bare 0.0."""
        prompt_tokens, completion_tokens = 1000, 500
        canonical, baseline = _drive_success_to_terminal(
            tier_name="local",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        assert canonical.final_attempt_cost == pytest.approx(0.0)
        assert canonical.cumulative_attempt_cost == pytest.approx(0.0)
        assert baseline.candidate_cost_usd == pytest.approx(0.0)

    def test_metered_and_free_local_diverge(self) -> None:
        """Cross-check: the same token counts produce a non-zero metered cost and a
        zero free_local cost — the fix is tier-sensitive, not a constant."""
        prompt_tokens, completion_tokens = 2000, 1000
        metered, _ = _drive_success_to_terminal(
            tier_name="cheap_cloud",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        free_local, _ = _drive_success_to_terminal(
            tier_name="local",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        assert metered.final_attempt_cost > free_local.final_attempt_cost
        assert free_local.final_attempt_cost == pytest.approx(0.0)
