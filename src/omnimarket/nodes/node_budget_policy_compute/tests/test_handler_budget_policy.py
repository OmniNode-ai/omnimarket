"""Tests for HandlerBudgetPolicy — pure deterministic budget policy evaluation."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_budget_policy_compute.handlers.handler_budget_policy import (
    HandlerBudgetPolicy,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumBudgetAction,
    EnumTaskPriority,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_request import (
    ModelBudgetPolicyRequest,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_usage import (
    ModelBudgetUsage,
)

_LIMITS = ModelBudgetLimits(max_tokens=1000, max_cost_usd=1.0, max_time_s=60.0)


def _req(
    *,
    tokens: int = 0,
    cost_usd: float = 0.0,
    elapsed_time_s: float = 0.0,
    priority: EnumTaskPriority = EnumTaskPriority.NORMAL,
    limits: ModelBudgetLimits = _LIMITS,
) -> ModelBudgetPolicyRequest:
    return ModelBudgetPolicyRequest(
        current_usage=ModelBudgetUsage(
            tokens=tokens,
            cost_usd=cost_usd,
            elapsed_time_s=elapsed_time_s,
        ),
        budget_limits=limits,
        task_priority=priority,
    )


@pytest.mark.unit
class TestHandlerBudgetPolicyContinue:
    def test_all_zero_usage_is_continue(self) -> None:
        result = HandlerBudgetPolicy().handle(_req())
        assert result.action == EnumBudgetAction.CONTINUE

    def test_well_below_warn_threshold_is_continue(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=500, cost_usd=0.4, elapsed_time_s=20.0)
        )
        assert result.action == EnumBudgetAction.CONTINUE

    def test_continue_has_empty_dimensions_exceeded(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=100))
        assert result.dimensions_exceeded == []

    def test_same_input_always_produces_identical_output(self) -> None:
        req = _req(tokens=300, cost_usd=0.3, elapsed_time_s=15.0)
        handler = HandlerBudgetPolicy()
        assert handler.handle(req).action == handler.handle(req).action
        assert handler.handle(req).reason == handler.handle(req).reason


@pytest.mark.unit
class TestHandlerBudgetPolicyWarn:
    def test_token_at_80_percent_triggers_warn(self) -> None:
        # 800/1000 = 0.80 — exactly at threshold
        result = HandlerBudgetPolicy().handle(_req(tokens=800))
        assert result.action == EnumBudgetAction.WARN

    def test_cost_at_85_percent_triggers_warn(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(cost_usd=0.85))
        assert result.action == EnumBudgetAction.WARN

    def test_time_at_90_percent_triggers_warn(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(elapsed_time_s=54.0))
        assert result.action == EnumBudgetAction.WARN

    def test_warn_names_the_approaching_dimension(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=850))
        assert "tokens" in result.dimensions_exceeded

    def test_warn_does_not_include_safe_dimensions(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=850, cost_usd=0.1))
        assert "cost_usd" not in result.dimensions_exceeded

    def test_warn_recommended_action_is_informational(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=850))
        assert result.recommended_action  # non-empty string
        assert "throttle" in result.recommended_action.lower()


@pytest.mark.unit
class TestHandlerBudgetPolicyThrottle:
    def test_token_at_100_percent_triggers_throttle(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=1000))
        assert result.action == EnumBudgetAction.THROTTLE

    def test_cost_over_100_percent_triggers_abort_for_normal_priority(self) -> None:
        # For NORMAL, abort_mult=1.0; ratio=1.05 > 1.0 → ABORT (not THROTTLE)
        result = HandlerBudgetPolicy().handle(_req(cost_usd=1.05))
        assert result.action == EnumBudgetAction.ABORT

    def test_throttle_lists_at_limit_dimension(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=1000))
        assert "tokens" in result.dimensions_exceeded

    def test_throttle_recommended_action_mentions_quality(self) -> None:
        result = HandlerBudgetPolicy().handle(_req(tokens=1000))
        assert "quality" in result.recommended_action.lower()

    def test_throttle_also_includes_warned_dimensions(self) -> None:
        # tokens throttled, cost just warned
        result = HandlerBudgetPolicy().handle(_req(tokens=1000, cost_usd=0.85))
        assert "tokens" in result.dimensions_exceeded
        assert "cost_usd" in result.dimensions_exceeded


@pytest.mark.unit
class TestHandlerBudgetPolicyAbort:
    def test_normal_priority_aborts_at_100_percent_exceeded(self) -> None:
        # For NORMAL, abort_multiplier=1.0 so ratio>=1.0 is throttle, not abort.
        # Abort requires ratio >= 1.0 * 1.0 but throttle fires at >= 1.0.
        # At exactly 1.0 → throttle; to abort for NORMAL we need ratio > 1.0.
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1001, priority=EnumTaskPriority.NORMAL)
        )
        assert result.action == EnumBudgetAction.ABORT

    def test_critical_priority_does_not_abort_at_100_percent(self) -> None:
        # CRITICAL multiplier is 1.2 so ratio=1.0 is only throttle.
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1000, priority=EnumTaskPriority.CRITICAL)
        )
        assert result.action == EnumBudgetAction.THROTTLE

    def test_critical_priority_aborts_above_120_percent(self) -> None:
        # abort fires at ratio > 1.2; tokens=1201 gives ratio=1.201
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1201, priority=EnumTaskPriority.CRITICAL)
        )
        assert result.action == EnumBudgetAction.ABORT

    def test_critical_priority_does_not_abort_at_or_below_120_percent(self) -> None:
        # ratio=1.2 exactly → throttle (abort requires strictly > 1.2)
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1200, priority=EnumTaskPriority.CRITICAL)
        )
        assert result.action == EnumBudgetAction.THROTTLE

    def test_abort_lists_exceeded_dimension(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1100, priority=EnumTaskPriority.NORMAL)
        )
        assert "tokens" in result.dimensions_exceeded

    def test_abort_recommended_action_says_stop(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1100, priority=EnumTaskPriority.NORMAL)
        )
        assert "stop" in result.recommended_action.lower()

    def test_abort_reason_includes_priority(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1100, priority=EnumTaskPriority.HIGH)
        )
        assert "HIGH" in result.reason

    def test_multiple_dimensions_aborted_all_listed(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1100, cost_usd=1.5, elapsed_time_s=70.0)
        )
        assert result.action == EnumBudgetAction.ABORT
        assert "tokens" in result.dimensions_exceeded
        assert "cost_usd" in result.dimensions_exceeded
        assert "elapsed_time_s" in result.dimensions_exceeded


@pytest.mark.unit
class TestHandlerBudgetPolicyPriorityVariants:
    def test_low_priority_aborts_same_as_normal(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1001, priority=EnumTaskPriority.LOW)
        )
        assert result.action == EnumBudgetAction.ABORT

    def test_high_priority_aborts_same_as_normal(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1001, priority=EnumTaskPriority.HIGH)
        )
        assert result.action == EnumBudgetAction.ABORT

    def test_priority_appears_in_warn_reason(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=850, priority=EnumTaskPriority.HIGH)
        )
        assert "HIGH" in result.reason

    def test_priority_appears_in_throttle_reason(self) -> None:
        result = HandlerBudgetPolicy().handle(
            _req(tokens=1000, priority=EnumTaskPriority.LOW)
        )
        assert "LOW" in result.reason


@pytest.mark.unit
class TestHandlerBudgetPolicyActionOrdering:
    def test_abort_beats_throttle(self) -> None:
        # One dim aborted, another at limit (throttle-level) — result must be ABORT.
        result = HandlerBudgetPolicy().handle(_req(tokens=1100, cost_usd=1.0))
        assert result.action == EnumBudgetAction.ABORT

    def test_throttle_beats_warn(self) -> None:
        # One dim throttled, another warned — result must be THROTTLE.
        result = HandlerBudgetPolicy().handle(_req(tokens=1000, cost_usd=0.85))
        assert result.action == EnumBudgetAction.THROTTLE
