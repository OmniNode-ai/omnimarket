# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_session_phase_orchestrator."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class ModelSessionPhaseEvaluation(BaseModel):
    """A phase evaluation result consumed from onex.evt.omnimarket.session-phase-evaluated.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    session_id: str  # string-id-ok: overseer correlation string
    phase_name: str
    action: Literal[
        "no_action", "budget_warning", "transition_required", "halt_required"
    ]
    reason: str
    next_phase: str | None = None
    budget_elapsed_pct: int = 0
    elapsed_seconds: float = 0.0
    cost_usd: float = 0.0
    budget_usd: float = 5.0


class ModelSessionPhaseOrchestratorInput(BaseModel):
    """Input envelope for HandlerSessionPhaseOrchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluations: tuple[ModelSessionPhaseEvaluation, ...]

    @model_validator(mode="before")
    @classmethod
    def _wrap_single_evaluation(cls, data: Any) -> Any:
        """Accept one evaluation exactly as the evaluator publishes it.

        ``session-phase-evaluated`` carries one evaluation per event, not a batch.
        A mapping with no ``evaluations`` key is that single evaluation; the batch
        shape stays valid for callers that already build it.
        """
        if isinstance(data, dict) and "evaluations" not in data:
            return {"evaluations": (data,)}
        return data


__all__ = [
    "ModelSessionPhaseEvaluation",
    "ModelSessionPhaseOrchestratorInput",
]
