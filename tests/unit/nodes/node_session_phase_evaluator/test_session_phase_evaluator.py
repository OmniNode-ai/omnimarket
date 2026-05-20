"""Tests for HandlerSessionPhaseEvaluator — pure deterministic phase evaluation."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_session_phase_evaluator.handlers.handler_session_phase_evaluator import (
    HandlerSessionPhaseEvaluator,
    ModelPhaseEvaluationRequest,
)


def _req(
    *,
    phase_name: str = "planning",
    max_duration_minutes: int = 60,
    elapsed_minutes: float = 0.0,
    exit_condition_statuses: dict[str, bool] | None = None,
    halt_threshold_pct: int = 100,
) -> ModelPhaseEvaluationRequest:
    return ModelPhaseEvaluationRequest(
        phase_name=phase_name,
        max_duration_minutes=max_duration_minutes,
        elapsed_minutes=elapsed_minutes,
        exit_condition_statuses=exit_condition_statuses or {},
        halt_threshold_pct=halt_threshold_pct,
    )


_handler = HandlerSessionPhaseEvaluator()


@pytest.mark.unit
class TestPhaseEvaluatorNoAction:
    def test_returns_no_action_when_in_budget(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=30.0))
        assert result.action == "no_action"

    def test_returns_no_action_at_50_pct_with_unsatisfied_conditions(self) -> None:
        result = _handler.handle(
            _req(
                elapsed_minutes=30.0,
                exit_condition_statuses={"tickets_done": False, "ci_green": False},
            )
        )
        assert result.action == "no_action"

    def test_budget_elapsed_pct_computed_correctly(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=30.0, max_duration_minutes=60))
        assert result.budget_elapsed_pct == 50

    def test_same_input_always_produces_identical_output(self) -> None:
        req = _req(elapsed_minutes=20.0)
        r1 = _handler.handle(req)
        r2 = _handler.handle(req)
        assert r1.action == r2.action
        assert r1.reason == r2.reason
        assert r1.budget_elapsed_pct == r2.budget_elapsed_pct


@pytest.mark.unit
class TestPhaseEvaluatorBudgetWarning:
    def test_evaluator_detects_budget_warning_at_80_pct(self) -> None:
        # 48/60 = 80%
        result = _handler.handle(_req(elapsed_minutes=48.0))
        assert result.action == "budget_warning"

    def test_budget_warning_at_90_pct(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=54.0))
        assert result.action == "budget_warning"

    def test_budget_warning_pct_reported(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=48.0))
        assert result.budget_elapsed_pct == 80

    def test_budget_warning_reason_mentions_phase(self) -> None:
        result = _handler.handle(_req(phase_name="execution", elapsed_minutes=48.0))
        assert "execution" in result.reason

    def test_budget_warning_does_not_fire_below_80_pct(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=47.9))
        assert result.action == "no_action"


@pytest.mark.unit
class TestPhaseEvaluatorTransitionRequired:
    def test_evaluator_detects_exit_conditions_met(self) -> None:
        result = _handler.handle(
            _req(
                elapsed_minutes=30.0,
                exit_condition_statuses={"tickets_done": True, "ci_green": True},
            )
        )
        assert result.action == "transition_required"

    def test_evaluator_detects_budget_exhausted(self) -> None:
        # 60/60 = 100% — halt_threshold_pct=100 so halt fires, not transition
        # Use halt_threshold_pct=101 to test pure budget-exhausted path via transition
        result = _handler.handle(
            _req(
                elapsed_minutes=60.0,
                halt_threshold_pct=101,
                exit_condition_statuses={"tickets_done": False},
            )
        )
        assert result.action == "transition_required"

    def test_partial_conditions_not_met_no_transition(self) -> None:
        result = _handler.handle(
            _req(
                elapsed_minutes=30.0,
                exit_condition_statuses={"tickets_done": True, "ci_green": False},
            )
        )
        assert result.action != "transition_required"

    def test_empty_conditions_dict_not_treated_as_all_met(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=30.0, exit_condition_statuses={}))
        assert result.action == "no_action"

    def test_transition_reason_mentions_phase(self) -> None:
        result = _handler.handle(
            _req(
                phase_name="review",
                elapsed_minutes=30.0,
                exit_condition_statuses={"approved": True},
            )
        )
        assert "review" in result.reason


@pytest.mark.unit
class TestPhaseEvaluatorHaltRequired:
    def test_evaluator_detects_halt_condition_at_100_pct(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=60.0, halt_threshold_pct=100))
        assert result.action == "halt_required"

    def test_halt_fires_at_custom_threshold(self) -> None:
        # halt_threshold_pct=90, elapsed=90% of budget
        result = _handler.handle(
            _req(
                elapsed_minutes=54.0,
                max_duration_minutes=60,
                halt_threshold_pct=90,
            )
        )
        assert result.action == "halt_required"

    def test_halt_takes_priority_over_exit_conditions_met(self) -> None:
        result = _handler.handle(
            _req(
                elapsed_minutes=60.0,
                halt_threshold_pct=100,
                exit_condition_statuses={"done": True},
            )
        )
        assert result.action == "halt_required"

    def test_halt_pct_reported_correctly(self) -> None:
        result = _handler.handle(_req(elapsed_minutes=60.0, halt_threshold_pct=100))
        assert result.budget_elapsed_pct == 100

    def test_halt_reason_mentions_phase(self) -> None:
        result = _handler.handle(
            _req(phase_name="deploy", elapsed_minutes=60.0, halt_threshold_pct=100)
        )
        assert "deploy" in result.reason

    def test_no_halt_just_below_threshold(self) -> None:
        # 59/60 = 98% < 100%
        result = _handler.handle(_req(elapsed_minutes=59.0, halt_threshold_pct=100))
        assert result.action != "halt_required"
