"""Pure deterministic session phase evaluator — no I/O, no side effects."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_WARN_THRESHOLD_PCT = 80
_DEFAULT_HALT_THRESHOLD_PCT = 100


class ModelPhaseEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal[
        "no_action", "budget_warning", "transition_required", "halt_required"
    ]
    reason: str
    next_phase: str | None = None
    budget_elapsed_pct: int = 0


class ModelPhaseEvaluationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase_name: str
    max_duration_minutes: int = Field(..., ge=1)
    elapsed_minutes: float = Field(..., ge=0.0)
    exit_condition_statuses: dict[str, bool]
    halt_threshold_pct: int = Field(_DEFAULT_HALT_THRESHOLD_PCT, ge=1)


def _elapsed_pct(elapsed_minutes: float, max_duration_minutes: int) -> int:
    return min(max(int((elapsed_minutes / max_duration_minutes) * 100), 0), 100)


def _all_conditions_met(exit_condition_statuses: dict[str, bool]) -> bool:
    if not exit_condition_statuses:
        return False
    return all(exit_condition_statuses.values())


class HandlerSessionPhaseEvaluator:
    """Evaluate session phase exit conditions and budget status."""

    def handle(self, request: ModelPhaseEvaluationRequest) -> ModelPhaseEvaluation:
        elapsed_pct = _elapsed_pct(
            request.elapsed_minutes, request.max_duration_minutes
        )

        if elapsed_pct >= request.halt_threshold_pct:
            return ModelPhaseEvaluation(
                action="halt_required",
                reason=(
                    f"Phase '{request.phase_name}' has exceeded halt threshold: "
                    f"{elapsed_pct}% >= {request.halt_threshold_pct}%."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if elapsed_pct >= _DEFAULT_HALT_THRESHOLD_PCT:
            return ModelPhaseEvaluation(
                action="transition_required",
                reason=(
                    f"Phase '{request.phase_name}' budget exhausted at {elapsed_pct}%."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if _all_conditions_met(request.exit_condition_statuses):
            return ModelPhaseEvaluation(
                action="transition_required",
                reason=(
                    f"All exit conditions satisfied for phase '{request.phase_name}'."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if elapsed_pct >= _WARN_THRESHOLD_PCT:
            return ModelPhaseEvaluation(
                action="budget_warning",
                reason=(
                    f"Phase '{request.phase_name}' has consumed {elapsed_pct}% of budget "
                    f"({request.elapsed_minutes:.1f}/{request.max_duration_minutes} min)."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        return ModelPhaseEvaluation(
            action="no_action",
            reason=(
                f"Phase '{request.phase_name}' within budget ({elapsed_pct}%) "
                "and exit conditions not yet met."
            ),
            budget_elapsed_pct=elapsed_pct,
        )


__all__ = [
    "HandlerSessionPhaseEvaluator",
    "ModelPhaseEvaluation",
    "ModelPhaseEvaluationRequest",
]
