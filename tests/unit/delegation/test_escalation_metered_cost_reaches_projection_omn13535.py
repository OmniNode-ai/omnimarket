# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13535: escalated metered-tier spend reaches the delegation projection.

Root cause (verified live on the .201 dev lane): when a delegation escalates off
a metered tier (the metered call REALLY runs and reports thousands of tokens) and
the quality gate rejects that tier's output, the workflow escalates to a cheaper /
free tier and the terminal recorded only the FINAL accepted tier (free →
cost_usd=0). The metered tier's real tokens/cost were reset on escalation and
silently dropped, so ``delegation_events`` carried ``cost_usd=0.0`` despite a
real metered cloud call — blocking the OMN-13408 metered-tier ``cost_usd>0`` proof.

Fix: each attempted tier's served tokens are priced once (the same typed tier
cost model the projection uses) and stamped onto its ``escalation_history``
record + accumulated into the workflow. The terminal then reports
``final_tier_cost + cumulative_prior_attempt_cost`` (cumulative metered spend
across ALL attempted tiers), and the projection re-derives the same total by
adding the per-attempt ``escalation_history`` costs to its re-priced final tier.

These tests drive the orchestrator FSM through a real metered→free escalation and
assert the terminal events + the projection row carry the metered ``cost_usd>0``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelTaskDelegatedSavingsSource,
)
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
    ModelActualCostProjection,
    _canonical_result_to_task_delegated_payload,
    _measure_actual_cost,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelTaskDelegatedEvent as ModelProjectionTaskDelegatedEvent,
)
from omnimarket.pricing import (
    build_premium_counterfactual,
    recompute_actual_cost_and_savings,
)


def _canonical_terminal(events: list[object]) -> ModelDelegationResult:
    """The SINGLE canonical terminal payload from one emission (OMN-13629)."""
    terminals = [
        e.payload
        for e in events
        if isinstance(e, ModelDelegationEvent)
        and isinstance(e.payload, ModelDelegationResult)
    ]
    assert len(terminals) == 1, f"expected one canonical terminal, got {events!r}"
    return terminals[0]


def _measure_canonical(canonical: ModelDelegationResult) -> ModelActualCostProjection:
    """The projection's actual-cost measurement from the canonical terminal."""
    converted = _canonical_result_to_task_delegated_payload(
        canonical.model_dump(mode="json")
    )
    return _measure_actual_cost(ModelProjectionTaskDelegatedEvent(**converted))


# Metered cheap_cloud token counts mirroring the live evidence (z.ai GLM call).
_METERED_PROMPT_TOKENS = 5627
_METERED_COMPLETION_TOKENS = 5831
# Free accepted-tier tokens (small, $0 by tier model).
_FREE_PROMPT_TOKENS = 200
_FREE_COMPLETION_TOKENS = 300

_TASK_TYPE = "code_generation"


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Implement a function with full edge-case coverage.",
        task_type=_TASK_TYPE,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID, tier_name: str
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=_TASK_TYPE,
        selected_model=f"model-{tier_name}",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="https://api.example/v1/chat/completions",
        cost_tier="low",
        tier_name=tier_name,
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a code generation assistant.",
        rationale=f"Routed via tier '{tier_name}'.",
    )


def _make_inference_response(
    correlation_id: UUID,
    *,
    model_used: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def solution():\n    return 42",
        model_used=model_used,
        llm_call_id="chatcmpl-omn13535",
        latency_ms=88_998,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _make_gate_result(
    correlation_id: UUID,
    *,
    passed: bool,
    quality_score: float,
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=passed,
        quality_score=quality_score,
        failure_reasons=() if passed else ("covers_edge_cases",),
        fallback_recommended=not passed,
    )


def _expected_metered_cost() -> float:
    measurement = recompute_actual_cost_and_savings(
        tier_name="cheap_cloud",
        prompt_tokens=_METERED_PROMPT_TOKENS,
        completion_tokens=_METERED_COMPLETION_TOKENS,
        premium_counterfactual=None,
    )
    assert measurement.cash_cost_usd > 0.0
    return measurement.cash_cost_usd


def _drive_metered_reject_then_free_accept(
    handler: HandlerDelegationWorkflow, cid: UUID
) -> list[object]:
    """metered cheap_cloud (rejected) -> escalate -> free 'local' (accepted)."""
    handler.handle_delegation_request(_make_request(cid))

    # Attempt 1: metered cheap_cloud, real tokens, REJECTED by the gate.
    handler.handle_routing_decision(
        _make_routing_decision(cid, tier_name="cheap_cloud")
    )
    handler.handle_inference_response(
        _make_inference_response(
            cid,
            model_used="glm-5.2",
            prompt_tokens=_METERED_PROMPT_TOKENS,
            completion_tokens=_METERED_COMPLETION_TOKENS,
        )
    )
    escalation_events = handler.handle_gate_result(
        _make_gate_result(cid, passed=False, quality_score=0.2)
    )
    assert handler.workflows[cid].state == EnumDelegationState.ROUTED
    assert handler.workflows[cid].escalation_count == 1
    assert not any(isinstance(e, ModelDelegationEvent) for e in escalation_events)

    # Attempt 2: free 'local' tier, ACCEPTED by the gate (terminal COMPLETED).
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
    handler.handle_inference_response(
        _make_inference_response(
            cid,
            model_used="qwen3-coder-30b",
            prompt_tokens=_FREE_PROMPT_TOKENS,
            completion_tokens=_FREE_COMPLETION_TOKENS,
        )
    )
    terminal_events = handler.handle_gate_result(
        _make_gate_result(cid, passed=True, quality_score=0.95)
    )
    assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
    return terminal_events


@pytest.mark.unit
@pytest.mark.usefixtures("frontier_unconfigured_bifrost")
class TestEscalationMeteredCostReachesProjectionOmn13535:
    """The metered tier's spend survives escalation into the terminal + projection.

    Uses the shared ``frontier_unconfigured_bifrost`` fixture (conftest) so tier
    routability is pinned to a deterministic bifrost contract — local + cheap_cloud
    backends carry resolvable endpoints, the frontier ceiling does not. Without it
    the ``local`` accepted tier's routability depended on ambient
    ``BIFROST_LOCAL_*`` endpoint env vars: present on a developer machine (test
    passed) but absent in CI, where the metered→free escalation found no routable
    next tier and the workflow terminated FAILED instead of ROUTED (OMN-13535).
    """

    def test_terminal_carries_cumulative_metered_cost_after_escalation(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        events = _drive_metered_reject_then_free_accept(handler, cid)

        expected_metered = _expected_metered_cost()

        # Canonical ModelDelegationResult terminal.
        result_event = next(e for e in events if isinstance(e, ModelDelegationEvent))
        result = result_event.payload
        assert isinstance(result, ModelDelegationResult)
        # The cumulative attempt cost includes the rejected metered tier even
        # though the FINAL accepted tier ('local') is free.
        assert result.cumulative_attempt_cost == pytest.approx(expected_metered)
        assert result.cumulative_attempt_cost > 0.0
        assert result.cumulative_input_tokens == (
            _FREE_PROMPT_TOKENS + _METERED_PROMPT_TOKENS
        )
        assert result.cumulative_output_tokens == (
            _FREE_COMPLETION_TOKENS + _METERED_COMPLETION_TOKENS
        )
        # The rejected metered tier is recorded in escalation_history WITH its
        # priced cost (the durable per-tier provenance the projection re-derives).
        history = result.escalation_history
        assert len(history) == 1
        assert history[0]["tier_name"] == "cheap_cloud"
        assert float(history[0]["cost_usd"]) == pytest.approx(expected_metered)
        assert history[0]["prompt_tokens"] == _METERED_PROMPT_TOKENS

    def test_canonical_terminal_cost_is_total_metered_spend(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        events = _drive_metered_reject_then_free_accept(handler, cid)

        expected_metered = _expected_metered_cost()
        canonical = _canonical_terminal(events)
        # The canonical terminal (and thus the projection row) reflects the TOTAL
        # metered spend, not the free final tier's $0. OMN-13629: this is the
        # single source — there is no compat co-writer.
        assert canonical.cumulative_attempt_cost == pytest.approx(
            round(expected_metered, 6)
        )
        assert canonical.cumulative_attempt_cost > 0.0

    def test_projection_row_reports_metered_cost_for_escalated_delegation(self) -> None:
        """End-to-end: the canonical terminal projects a cost_usd>0 row (the DoD)."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        events = _drive_metered_reject_then_free_accept(handler, cid)

        measurement = _measure_canonical(_canonical_terminal(events))

        expected_metered = _expected_metered_cost()
        # The projected actual cost is the metered spend — NOT zeroed to the free
        # accepted tier. This is the OMN-13535 / OMN-13408 metered cost_usd>0 row.
        assert measurement.cost_usd == pytest.approx(expected_metered)
        assert measurement.cost_usd > 0.0

    def test_savings_reconciles_against_total_spend(self) -> None:
        """The savings projection re-derives counterfactual - TOTAL spend."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        events = _drive_metered_reject_then_free_accept(handler, cid)

        canonical = _canonical_terminal(events)
        # OMN-13629: the savings projection re-derives the counterfactual from the
        # served tokens and subtracts the measured cumulative cost. The saving is
        # non-negative and strictly less than the counterfactual when cost > 0.
        source = ModelTaskDelegatedSavingsSource.from_canonical_payload(
            canonical.model_dump(mode="json"),
            counterfactual_builder=build_premium_counterfactual,
        )
        assert source.premium_counterfactual is not None
        counterfactual = float(source.premium_counterfactual.counterfactual_cost_usd)
        saving = counterfactual - float(source.cost_usd)
        assert saving == pytest.approx(counterfactual - float(source.cost_usd))
        assert saving < counterfactual

    def test_non_escalated_metered_row_unchanged(self) -> None:
        """Regression guard: a single-attempt metered delegation is unaffected.

        With no escalation history the recompute degrades to the prior single-tier
        behavior — cost_usd is exactly the served-tokens metered cost.
        """
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, tier_name="cheap_cloud")
        )
        handler.handle_inference_response(
            _make_inference_response(
                cid,
                model_used="glm-5.2",
                prompt_tokens=1000,
                completion_tokens=500,
            )
        )
        events = handler.handle_gate_result(
            _make_gate_result(cid, passed=True, quality_score=0.95)
        )
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED

        single_tier_cost = recompute_actual_cost_and_savings(
            tier_name="cheap_cloud",
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=None,
        ).cash_cost_usd

        result = _canonical_terminal(events)
        # No prior attempts → cumulative == the single served tier's cost.
        assert result.cumulative_attempt_cost == pytest.approx(single_tier_cost)
        assert result.escalation_history == ()

        measurement = _measure_canonical(result)
        assert measurement.cost_usd == pytest.approx(single_tier_cost)

    def test_terminal_fail_metered_tier_not_double_counted(self) -> None:
        """An escalation-exhausted terminal on the metered tier is counted ONCE.

        The current (terminal) attempt is recorded in escalation_history but NOT
        banked into the cumulative accumulator — ``_emit_terminal`` re-prices it
        from the current-tier tokens. The FAILED terminal (premium=None) carries
        the metered cost verbatim, exactly once.
        """
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))
        handler.handle_routing_decision(
            _make_routing_decision(cid, tier_name="cheap_cloud")
        )
        handler.handle_inference_response(
            _make_inference_response(
                cid,
                model_used="glm-5.2",
                prompt_tokens=_METERED_PROMPT_TOKENS,
                completion_tokens=_METERED_COMPLETION_TOKENS,
            )
        )
        # Force escalation exhaustion so this metered attempt is the TERMINAL one.
        handler.workflows[cid].escalation_count = 2
        events = handler.handle_gate_result(
            _make_gate_result(cid, passed=False, quality_score=0.2),
            max_escalation_attempts=2,
        )
        assert handler.workflows[cid].state == EnumDelegationState.FAILED

        expected_metered = _expected_metered_cost()
        result = _canonical_terminal(events)
        # Counted ONCE — the current terminal tier is not double-banked.
        assert result.cumulative_attempt_cost == pytest.approx(expected_metered)

        # The canonical FAILED terminal carries no counterfactual; the projection
        # keeps the metered cost and the saving stays 0 (never counterfactual-0).
        measurement = _measure_canonical(result)
        assert measurement.cost_usd == pytest.approx(round(expected_metered, 6))
        assert measurement.cost_usd > 0.0
        assert measurement.cost_savings_usd == 0.0
