# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase orchestrator — routes evaluation results to transition or halt.

ORCHESTRATOR nodes emit events[] and intents[] only.
They CANNOT set result. Returning result raises ValueError.

Related:
    - OMN-11232: Task 8: Create node_session_phase_orchestrator (ORCHESTRATOR)
    - OMN-11224: Session phase control loop epic
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_TOPIC_TRANSITION = "onex.cmd.omnimarket.session-phase-transition.v1"  # onex-topic-allow: pending contract auto-wiring
_TOPIC_HALT = "onex.cmd.omnimarket.session-halt-required.v1"  # onex-topic-allow: pending contract auto-wiring

PhaseAction = Literal[
    "no_action", "budget_warning", "transition_required", "halt_required"
]


class ModelPhaseEvaluationResult(BaseModel):
    """Payload from node_session_phase_evaluator via session-phase-evaluated event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: PhaseAction
    reason: str
    next_phase: str | None = None
    budget_elapsed_pct: int = 0
    phase_name: str = ""
    session_id: str = ""
    correlation_id: str = ""


class HandlerSessionPhaseOrchestrator:
    """Route phase evaluation results to transition or halt commands.

    ORCHESTRATOR contract: output dict contains only 'events' and/or 'intents'.
    Setting 'result' in the output dict violates the contract and raises ValueError.
    """

    @staticmethod
    def _guard_no_result(output: dict[str, Any]) -> dict[str, Any]:
        if "result" in output:
            raise ValueError(
                "ORCHESTRATOR contract violation: 'result' must not be set. "
                "Emit events[] and intents[] only."
            )
        return output

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Handle a phase evaluation result and emit routing commands.

        Returns a dict with 'events' key only (never 'result').
        no_action and budget_warning produce an empty events list.
        """
        evaluation = ModelPhaseEvaluationResult(**input_data)
        events = self._route(evaluation)
        output: dict[str, Any] = {"events": events}
        return self._guard_no_result(output)

    def _route(self, evaluation: ModelPhaseEvaluationResult) -> list[dict[str, Any]]:
        action = evaluation.action

        if action == "transition_required":
            logger.info(
                "Phase transition required: session=%s phase=%s next=%s reason=%s",
                evaluation.session_id,
                evaluation.phase_name,
                evaluation.next_phase,
                evaluation.reason,
            )
            return [
                {
                    "topic": _TOPIC_TRANSITION,
                    "payload": {
                        "session_id": evaluation.session_id,
                        "correlation_id": evaluation.correlation_id,
                        "phase_name": evaluation.phase_name,
                        "next_phase": evaluation.next_phase,
                        "budget_elapsed_pct": evaluation.budget_elapsed_pct,
                        "reason": evaluation.reason,
                    },
                }
            ]

        if action == "halt_required":
            logger.warning(
                "Phase halt required: session=%s phase=%s reason=%s",
                evaluation.session_id,
                evaluation.phase_name,
                evaluation.reason,
            )
            return [
                {
                    "topic": _TOPIC_HALT,
                    "payload": {
                        "session_id": evaluation.session_id,
                        "correlation_id": evaluation.correlation_id,
                        "phase_name": evaluation.phase_name,
                        "budget_elapsed_pct": evaluation.budget_elapsed_pct,
                        "reason": evaluation.reason,
                    },
                }
            ]

        # no_action or budget_warning: no routing command emitted
        logger.debug(
            "Phase evaluation action=%s — no routing command emitted: session=%s phase=%s",
            action,
            evaluation.session_id,
            evaluation.phase_name,
        )
        return []


__all__ = [
    "HandlerSessionPhaseOrchestrator",
    "ModelPhaseEvaluationResult",
]
