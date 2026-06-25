# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13408 (emitter hoist): a FAILED/escalation terminal whose TOP-LEVEL cost
is null/zero must HOIST the winning metered escalation_history tier's cost.

Live root cause (clean-room CID 1f969398, dev lane 2026-06-24, two-strike
diagnosis docs/evidence/2026-06-24-p0-tail/13408-two-strike-diagnosis.md): a
FAILED, escalation_count=1 terminal on the metered ``cheap_cloud`` (glm-5.2)
ladder emitted TOP-LEVEL ``cost_usd=None`` / ``cost_tier_name=None`` /
``tokens_*=None`` while the real metered spend (``cost_usd=0.001098``,
99/450 served tokens) lived ONLY in ``escalation_history[1]``. The projection
then short-circuited on the null top-level tier and persisted ``cost_usd=0.0``.

The carry/cumulative fixes (OMN-13408 dimensions 1-3, OMN-13535) populate the
top level WHEN ``workflow.current_tier_name`` / ``workflow.inference_*`` are
intact at terminal-build time. This guards the residual shape where they are
NOT — the metered spend survives only in ``escalation_history``. ``_emit_terminal``
must, on a FAILED terminal whose measured top-level cost is null/zero, hoist the
LAST metered ``escalation_history`` tier's cost_usd + cost_tier_name +
cost_tier_type + tokens_input + tokens_output into the top-level terminal fields.

The OMN-13535 no-double-count invariant is preserved: the hoist REPLACES the
zero with the already-priced authoritative value once; it never re-adds. A
``free_local`` FAILED terminal (no metered tier in history) honestly stays 0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    TerminalEmissionInputs,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.pricing import recompute_actual_cost_and_savings

# The live 1f969398 escalation_history shape: a free local attempt that failed
# QG and escalated, then the metered cheap_cloud (glm-5.2) attempt that itself
# failed QG and exhausted escalation -> FAILED terminal. The metered cost lives
# ONLY in the final history entry; the top level is null/zero.
_CHEAP_CLOUD = "cheap_cloud"
_LOCAL = "local"
_PROMPT_TOKENS = 99
_COMPLETION_TOKENS = 450


def _metered_history() -> tuple[dict[str, object], ...]:
    """escalation_history with a free local then a metered cheap_cloud attempt,
    each already priced by ``_record_escalation_attempt`` (cost stamped on)."""
    metered = recompute_actual_cost_and_savings(
        tier_name=_CHEAP_CLOUD,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        premium_counterfactual=None,
    )
    assert metered.cash_cost_usd > 0.0, "fixture requires a metered tier with cost > 0"
    return (
        {
            "tier_name": _LOCAL,
            "model_used": "Qwen3.6-35B-A3B",
            "quality_score": 0.2,
            "cost_usd": 0.0,
            "prompt_tokens": 107,
            "completion_tokens": 330,
            "latency_ms": 400,
            "fallback_recommended": True,
            "attempted_at": datetime.now(UTC).isoformat(),
        },
        {
            "tier_name": _CHEAP_CLOUD,
            "model_used": "glm-5.2",
            "quality_score": 0.3,
            "cost_usd": metered.cash_cost_usd,
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _COMPLETION_TOKENS,
            "latency_ms": 1200,
            "fallback_recommended": True,
            "attempted_at": datetime.now(UTC).isoformat(),
        },
    )


def _null_top_level_failed_inputs(
    *, history: tuple[dict[str, object], ...]
) -> TerminalEmissionInputs:
    """A FAILED terminal whose TOP-LEVEL cost/tokens/tier are null/zero — the
    live 1f969398 residual shape — but whose escalation_history carries the real
    metered spend on its final entry."""
    return TerminalEmissionInputs(
        completed=False,
        correlation_id=uuid4(),
        task_type="reasoning",
        model_used="glm-5.2",
        endpoint_url="https://api.z.ai/api/coding/paas/v4/chat/completions",
        content="",
        quality_passed=False,
        quality_score=0.0,
        latency_ms=1200,
        # Residual shape: top-level served tokens lost, only history carries them.
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        fallback_to_claude=True,
        failure_reason="score_below_required_bar",
        tokens_to_compliance=0,
        compliance_attempts=1,
        # Residual shape: serving tier name lost from the top-level inputs.
        cost_tier_name="",
        premium_counterfactual=None,
        escalation_count=1,
        escalation_history=history,
        terminal_failure_reason="no_higher_tier_available",
        routing_tiers_hash=None,
        escalation_config_hash=None,
        attempts_count=2,
        model_name="glm-5.2",
        session_id=None,
        quality_gates_checked=["length", "refusal", "markers"],
        quality_gates_failed=["score_below_required_bar"],
        llm_call_id="chatcmpl-omn13408-hoist",
        context_pack_hash="",
        # No prior-attempt banking on this residual shape (the metered cost was
        # not banked into the cumulative accumulators — it survives only in
        # escalation_history, which is exactly the bug condition this guards).
        prior_attempt_cost_usd=0.0,
        prior_attempt_prompt_tokens=0,
        prior_attempt_completion_tokens=0,
    )


@pytest.mark.unit
class TestTerminalCostHoistOmn13408:
    def test_failed_metered_terminal_hoists_history_cost_to_top_level(self) -> None:
        """RED before / GREEN after: a FAILED metered escalation terminal whose
        TOP-LEVEL cost/tier/tokens are null/zero must HOIST the winning metered
        escalation_history tier's cost_usd + cost_tier_name + cost_tier_type +
        tokens into the top-level terminal fields."""
        handler = HandlerDelegationWorkflow()
        history = _metered_history()
        events = handler._emit_terminal(_null_top_level_failed_inputs(history=history))

        compat = next(e for e in events if isinstance(e, ModelTaskDelegatedEvent))
        canonical = next(
            e.payload for e in events if isinstance(e, ModelDelegationEvent)
        )

        expected = recompute_actual_cost_and_savings(
            tier_name=_CHEAP_CLOUD,
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            premium_counterfactual=None,
        )
        assert expected.cash_cost_usd > 0.0

        # Compat ModelTaskDelegatedEvent (co-writer of the projection row).
        assert Decimal(str(compat.cost_usd)) == Decimal(str(expected.cash_cost_usd))
        assert compat.cost_usd > 0.0
        assert compat.cost_tier_name == _CHEAP_CLOUD
        assert compat.cost_tier_type == "metered"
        assert compat.tokens_input == _PROMPT_TOKENS
        assert compat.tokens_output == _COMPLETION_TOKENS
        # No counterfactual on the failure path -> saving stays 0 (never
        # counterfactual-minus-0).
        assert Decimal(str(compat.cost_savings_usd)) == Decimal("0")

        # Canonical ModelDelegationResult must agree (single-source builder).
        assert canonical.cumulative_attempt_cost > 0.0
        assert canonical.final_attempt_cost == pytest.approx(expected.cash_cost_usd)
        assert canonical.cumulative_input_tokens == _PROMPT_TOKENS
        assert canonical.cumulative_output_tokens == _COMPLETION_TOKENS

    def test_no_double_count_when_top_level_already_populated(self) -> None:
        """OMN-13535 invariant: when the top-level cost is ALREADY non-zero (the
        normal single-hop path where current_tier_name/inference_* are intact),
        the hoist must NOT fire — the authoritative summed total is preserved
        verbatim, not re-added from escalation_history."""
        handler = HandlerDelegationWorkflow()
        history = _metered_history()
        inputs = _null_top_level_failed_inputs(history=history)
        # Populate the top level as the intact path would: serving tier known,
        # served tokens present.
        populated = TerminalEmissionInputs(
            **{
                **inputs.__dict__,
                "cost_tier_name": _CHEAP_CLOUD,
                "prompt_tokens": _PROMPT_TOKENS,
                "completion_tokens": _COMPLETION_TOKENS,
                "total_tokens": _PROMPT_TOKENS + _COMPLETION_TOKENS,
            }
        )
        events = handler._emit_terminal(populated)
        compat = next(e for e in events if isinstance(e, ModelTaskDelegatedEvent))

        expected = recompute_actual_cost_and_savings(
            tier_name=_CHEAP_CLOUD,
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            premium_counterfactual=None,
        )
        # final-tier cost (priced from the top-level tokens) + prior(0) = final.
        # NOT final + escalation_history(final) = double count.
        assert Decimal(str(compat.cost_usd)) == Decimal(str(expected.cash_cost_usd))
        assert compat.tokens_input == _PROMPT_TOKENS
        assert compat.tokens_output == _COMPLETION_TOKENS

    def test_failed_free_local_terminal_stays_zero(self) -> None:
        """A FAILED terminal with NO metered tier in escalation_history (free
        local only) stays honestly 0 — the hoist finds no metered winner to
        hoist, so cost_usd is 0 by the cost model, not a silent passthrough."""
        handler = HandlerDelegationWorkflow()
        free_only_history: tuple[dict[str, object], ...] = (
            {
                "tier_name": _LOCAL,
                "model_used": "Qwen3.6-35B-A3B",
                "quality_score": 0.2,
                "cost_usd": 0.0,
                "prompt_tokens": 120,
                "completion_tokens": 900,
                "latency_ms": 400,
                "fallback_recommended": True,
                "attempted_at": datetime.now(UTC).isoformat(),
            },
        )
        events = handler._emit_terminal(
            _null_top_level_failed_inputs(history=free_only_history)
        )
        compat = next(e for e in events if isinstance(e, ModelTaskDelegatedEvent))
        assert Decimal(str(compat.cost_usd)) == Decimal("0")
        assert Decimal(str(compat.cost_savings_usd)) == Decimal("0")
