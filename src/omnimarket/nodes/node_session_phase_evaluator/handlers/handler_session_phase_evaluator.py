"""Pure deterministic session phase evaluator — no I/O, no side effects."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_WARN_THRESHOLD_PCT = 80
_DEFAULT_HALT_THRESHOLD_PCT = 100


class ModelPhaseEvaluation(BaseModel):
    """Evaluation result published on ``session-phase-evaluated``.

    Carries the session identity the orchestrator needs to route it, so the wire
    payload is accepted by ``node_session_phase_orchestrator`` as published.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    correlation_id: UUID
    phase_name: str
    action: Literal[
        "no_action", "budget_warning", "transition_required", "halt_required"
    ]
    reason: str
    next_phase: str | None = None
    budget_elapsed_pct: int = 0
    elapsed_seconds: float = 0.0


class ModelPhaseEvaluationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    correlation_id: UUID
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

    @staticmethod
    def _evaluation(
        request: ModelPhaseEvaluationRequest,
        *,
        action: Literal[
            "no_action", "budget_warning", "transition_required", "halt_required"
        ],
        reason: str,
        budget_elapsed_pct: int,
    ) -> ModelPhaseEvaluation:
        return ModelPhaseEvaluation(
            session_id=request.session_id,
            correlation_id=request.correlation_id,
            phase_name=request.phase_name,
            action=action,
            reason=reason,
            budget_elapsed_pct=budget_elapsed_pct,
            elapsed_seconds=request.elapsed_minutes * 60.0,
        )

    def handle(self, request: ModelPhaseEvaluationRequest) -> ModelPhaseEvaluation:
        elapsed_pct = _elapsed_pct(
            request.elapsed_minutes, request.max_duration_minutes
        )

        if elapsed_pct >= request.halt_threshold_pct:
            return self._evaluation(
                request,
                action="halt_required",
                reason=(
                    f"Phase '{request.phase_name}' has exceeded halt threshold: "
                    f"{elapsed_pct}% >= {request.halt_threshold_pct}%."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if elapsed_pct >= _DEFAULT_HALT_THRESHOLD_PCT:
            return self._evaluation(
                request,
                action="transition_required",
                reason=(
                    f"Phase '{request.phase_name}' budget exhausted at {elapsed_pct}%."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if _all_conditions_met(request.exit_condition_statuses):
            return self._evaluation(
                request,
                action="transition_required",
                reason=(
                    f"All exit conditions satisfied for phase '{request.phase_name}'."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        if elapsed_pct >= _WARN_THRESHOLD_PCT:
            return self._evaluation(
                request,
                action="budget_warning",
                reason=(
                    f"Phase '{request.phase_name}' has consumed {elapsed_pct}% of budget "
                    f"({request.elapsed_minutes:.1f}/{request.max_duration_minutes} min)."
                ),
                budget_elapsed_pct=elapsed_pct,
            )

        return self._evaluation(
            request,
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
