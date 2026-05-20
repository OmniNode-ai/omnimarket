# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_session_phase_orchestrator."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
    elapsed_seconds: float = 0.0
    cost_usd: float = 0.0
    budget_usd: float = 5.0


class ModelSessionPhaseOrchestratorInput(BaseModel):
    """Input envelope for HandlerSessionPhaseOrchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluations: tuple[ModelSessionPhaseEvaluation, ...]


__all__ = [
    "ModelSessionPhaseEvaluation",
    "ModelSessionPhaseOrchestratorInput",
]
