# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13335 — escalation terminal must emit (no negative-savings ValidationError).

Live finding (CID 67d2bfc8): an escalating ``code_generation`` delegation walks
the ladder, ``delegation-escalation-triggered.v1`` fires, the higher-tier
inference RESPONSE arrives — but the escalation silently produces NO terminal, so
an escalated delegation can never yield a terminal with ``escalation_count >= 1``.

Root cause (environment-independent): the orchestrator's single terminal builder
``_emit_terminal`` computes

    total_savings_usd = cost.cost_savings_usd - prior_attempt_cost_usd   (line ~1669)

On an escalation that ran a *metered* prior tier (e.g. cheap_cloud GLM) and then
landed on a terminal whose final-tier saving is smaller than that prior metered
spend, this subtraction goes NEGATIVE. The builder then constructs the compat
``ModelTaskDelegatedEvent`` with that negative ``cost_savings_usd`` — but the core
wire DTO pins ``cost_savings_usd`` with ``ge=0.0``. Construction raises
``ValidationError``, crashing the dispatcher with NO terminal emitted: the silent
terminal loss the live run observed.

Fix: clamp ``total_savings_usd`` to its honest floor of ``0.0`` — an escalation
that burned metered budget did not "save" negative money (that spend is already
reflected in ``cost_usd``). A valid terminal is then ALWAYS emitted.

These tests are hermetic (no live routing config / secret dependence):

1. ``_emit_terminal`` is exercised DIRECTLY with constructed inputs whose prior
   metered spend exceeds the final-tier saving — the exact negative-savings
   condition — asserting it emits a terminal with ``cost_savings_usd >= 0``
   instead of raising.
2. The full orchestrator drive (metered cheap_cloud rejected -> escalate -> free
   local accepted) is pinned to the deterministic ``frontier_unconfigured_bifrost``
   fixture (the same CI-stable fixture OMN-13535 uses) and asserts the escalation
   terminal emits with ``escalation_count >= 1`` and non-negative savings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    TerminalEmissionInputs,
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

_TASK_TYPE = "code_generation"
# Real metered GLM token counts mirroring the live evidence — these price to a
# positive cash cost (~$0.0014) that, subtracted from a small final-tier saving,
# drives the pre-clamp savings negative.
_METERED_PROMPT_TOKENS = 5627
_METERED_COMPLETION_TOKENS = 5831
_FREE_PROMPT_TOKENS = 200
_FREE_COMPLETION_TOKENS = 300


def _terminal_inputs_with_metered_prior(cid: UUID) -> TerminalEmissionInputs:
    """A COMPLETED terminal whose metered prior-tier spend exceeds the final
    free tier's counterfactual saving — forcing the pre-clamp subtraction
    negative. No premium_counterfactual on the final tier means
    ``cost.cost_savings_usd == 0.0``, so ``0.0 - prior_attempt_cost_usd < 0``.
    """
    return TerminalEmissionInputs(
        completed=True,
        correlation_id=cid,
        task_type=_TASK_TYPE,
        model_used="qwen3-coder-30b",
        endpoint_url="http://local.test:8000/v1/chat/completions",
        content="def add(a, b):\n    return a + b\n",
        quality_passed=True,
        quality_score=0.95,
        latency_ms=42,
        prompt_tokens=_FREE_PROMPT_TOKENS,
        completion_tokens=_FREE_COMPLETION_TOKENS,
        total_tokens=_FREE_PROMPT_TOKENS + _FREE_COMPLETION_TOKENS,
        fallback_to_claude=False,
        failure_reason="",
        tokens_to_compliance=_FREE_PROMPT_TOKENS + _FREE_COMPLETION_TOKENS,
        compliance_attempts=1,
        # Final (free local) tier: no premium counterfactual -> saving 0.0.
        cost_tier_name="local",
        premium_counterfactual=None,
        escalation_count=1,
        escalation_history=(),
        terminal_failure_reason=None,
        routing_tiers_hash=None,
        escalation_config_hash=None,
        attempts_count=2,
        model_name="qwen3-coder-30b",
        session_id=None,
        quality_gates_checked=["length", "refusal", "markers"],
        quality_gates_failed=[],
        llm_call_id="chatcmpl-omn13335",
        context_pack_hash="",
        # The metered cheap_cloud tier that was rejected before escalating —
        # priced to a real positive cost. This is what drives savings negative.
        prior_attempt_cost_usd=0.0014,
        prior_attempt_prompt_tokens=_METERED_PROMPT_TOKENS,
        prior_attempt_completion_tokens=_METERED_COMPLETION_TOKENS,
    )


@pytest.mark.unit
class TestEmitTerminalClampsNegativeSavingsOmn13335:
    """``_emit_terminal`` must clamp negative savings, never crash the terminal."""

    def test_emit_terminal_with_metered_prior_emits_non_negative_savings(
        self,
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        # Must NOT raise — the live gap raised ValidationError on the legacy compat
        # event's ge=0.0 cost_savings_usd, losing the terminal entirely. OMN-13629
        # removed that event; the savings clamp is now an internal honest floor.
        events = handler._emit_terminal(_terminal_inputs_with_metered_prior(cid))

        terminals = [
            e.payload
            for e in events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        ]
        assert len(terminals) == 1, (
            "the terminal builder must emit exactly one canonical terminal (the "
            "live gap crashed here on negative savings, emitting none)"
        )
        terminal = terminals[0]
        # The metered spend still surfaces as positive cumulative terminal cost,
        # and the escalation that occurred is recorded.
        assert terminal.cumulative_attempt_cost > 0.0, (
            "the metered escalation spend must surface as positive terminal cost"
        )
        assert terminal.escalation_count == 1, (
            "the terminal must carry the escalation_count it was given"
        )
        assert terminal.quality_passed is True
        # The derived saving the projection materializes is non-negative (the
        # honest floor) — never negative, never a terminal-suppressing crash.
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            ModelTaskDelegatedEvent,
            _canonical_result_to_task_delegated_payload,
            _measure_actual_cost,
        )

        converted = _canonical_result_to_task_delegated_payload(
            terminal.model_dump(mode="json")
        )
        measurement = _measure_actual_cost(ModelTaskDelegatedEvent(**converted))
        assert measurement.cost_savings_usd >= 0.0


def _make_request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Implement a function with full edge-case coverage.",
        task_type=_TASK_TYPE,
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(cid: UUID, tier_name: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
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


def _make_inference(
    cid: UUID, *, model_used: str, prompt_tokens: int, completion_tokens: int
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="def solution():\n    return 42",
        model_used=model_used,
        llm_call_id="chatcmpl-omn13335",
        latency_ms=88_998,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _make_gate_result(
    cid: UUID, *, passed: bool, quality_score: float
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=passed,
        quality_score=quality_score,
        failure_reasons=() if passed else ("covers_edge_cases",),
        fallback_recommended=not passed,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("frontier_unconfigured_bifrost")
class TestEscalatedTerminalEmitsOmn13335:
    """The full orchestrator drive: a metered tier escalates and the resulting
    terminal emits with escalation_count >= 1 and non-negative savings.

    Pinned to the deterministic ``frontier_unconfigured_bifrost`` fixture (the
    CI-stable fixture OMN-13535 uses) so tier routability does not depend on
    ambient bifrost endpoint / secret env state.
    """

    def test_escalated_completed_terminal_emits_with_non_negative_savings(
        self,
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(cid))

        # Attempt 1: metered cheap_cloud, real tokens, REJECTED by the gate.
        handler.handle_routing_decision(
            _make_routing_decision(cid, tier_name="cheap_cloud")
        )
        handler.handle_inference_response(
            _make_inference(
                cid,
                model_used="glm-5.2",
                prompt_tokens=_METERED_PROMPT_TOKENS,
                completion_tokens=_METERED_COMPLETION_TOKENS,
            )
        )
        handler.handle_gate_result(
            _make_gate_result(cid, passed=False, quality_score=0.2)
        )
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED
        assert handler.workflows[cid].escalation_count == 1

        # Attempt 2: free 'local' tier, ACCEPTED by the gate (terminal COMPLETED).
        # Must NOT raise (the live gap crashed the terminal on negative savings).
        handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))
        handler.handle_inference_response(
            _make_inference(
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
        terminals = [
            e.payload
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        ]
        assert len(terminals) == 1, (
            "an escalated delegation must emit exactly one canonical terminal"
        )
        terminal = terminals[0]
        assert terminal.escalation_count >= 1, (
            "an escalation terminal must carry escalation_count >= 1"
        )
        # The metered prior tier's spend still surfaces in the cumulative cost.
        assert terminal.cumulative_attempt_cost > 0.0
        # The derived saving the projection materializes is non-negative.
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            ModelTaskDelegatedEvent,
            _canonical_result_to_task_delegated_payload,
            _measure_actual_cost,
        )

        converted = _canonical_result_to_task_delegated_payload(
            terminal.model_dump(mode="json")
        )
        measurement = _measure_actual_cost(ModelTaskDelegatedEvent(**converted))
        assert measurement.cost_savings_usd >= 0.0
