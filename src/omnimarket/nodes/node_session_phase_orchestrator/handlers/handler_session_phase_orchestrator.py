# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase orchestrator handler.

Tick-driven orchestrator for session phase lifecycle. Routes evaluation results
to downstream commands. Topics are read from contract.yaml — never hardcoded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.nodes.node_session_phase_orchestrator.models.model_orchestrator_input import (
    ModelSessionPhaseEvaluation,
    ModelSessionPhaseOrchestratorInput,
)
from omnimarket.nodes.node_session_phase_orchestrator.models.model_orchestrator_result import (
    ModelSessionPhaseCommand,
    ModelSessionPhaseOrchestratorResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["orchestrator"]

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_publish_topics() -> dict[str, str]:
    """Load publish topic names from contract.yaml."""
    if not _CONTRACT_PATH.exists():
        msg = f"contract.yaml not found at {_CONTRACT_PATH}"
        raise RuntimeError(msg)
    with _CONTRACT_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    topics: list[str] = (data.get("event_bus", {}) or {}).get(
        "publish_topics", []
    ) or []
    result: dict[str, str] = {}
    for topic in topics:
        if "session-phase-transition" in topic:
            result["transition"] = topic
        elif "session-halt-required" in topic:
            result["halt"] = topic
        elif "session-orchestrator-tick-completed" in topic:
            result["tick_completed"] = topic
    required = {"transition", "halt", "tick_completed"}
    missing = required - result.keys()
    if missing:
        msg = f"contract.yaml missing publish topics: {missing}"
        raise RuntimeError(msg)
    return result


_TOPICS = _load_publish_topics()
_TOPIC_TRANSITION = _TOPICS["transition"]
_TOPIC_HALT = _TOPICS["halt"]
_TOPIC_TICK_COMPLETED = _TOPICS["tick_completed"]

_CMD_TYPE_TRANSITION = "omnimarket.session-phase-transition"
_CMD_TYPE_HALT = "omnimarket.session-halt-required"
_CMD_TYPE_TICK_COMPLETED = "omnimarket.session-orchestrator-tick-completed"


class HandlerSessionPhaseOrchestrator:
    """Route phase evaluation results to transition or halt commands."""

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "orchestrator"

    def handle(
        self, input_envelope: ModelSessionPhaseOrchestratorInput
    ) -> ModelSessionPhaseOrchestratorResult:
        if not input_envelope.evaluations:
            msg = "evaluations must be non-empty"
            raise ValueError(msg)

        # Use correlation_id from the first evaluation; all should share one tick
        correlation_id = input_envelope.evaluations[0].correlation_id

        commands: list[ModelSessionPhaseCommand] = []
        tick_outcome: Literal[
            "no_action", "transition_dispatched", "halt_dispatched", "warning_delegated"
        ] = "no_action"

        for evaluation in input_envelope.evaluations:
            cmd, outcome = self._route_evaluation(evaluation)
            if cmd is not None:
                commands.append(cmd)
                # halt takes priority over transition
                if tick_outcome == "no_action" or outcome == "halt_dispatched":
                    tick_outcome = outcome

        # Always emit tick-completed so consumers can confirm the orchestrator ran
        commands.append(
            ModelSessionPhaseCommand(
                topic=_TOPIC_TICK_COMPLETED,
                command_type=_CMD_TYPE_TICK_COMPLETED,
                payload={
                    "correlation_id": str(correlation_id),
                    "tick_outcome": tick_outcome,
                    "evaluation_count": len(input_envelope.evaluations),
                },
            )
        )

        return ModelSessionPhaseOrchestratorResult(
            correlation_id=correlation_id,
            commands=tuple(commands),
            tick_outcome=tick_outcome,
        )

    def _route_evaluation(
        self, evaluation: ModelSessionPhaseEvaluation
    ) -> tuple[
        ModelSessionPhaseCommand | None,
        Literal[
            "no_action", "transition_dispatched", "halt_dispatched", "warning_delegated"
        ],
    ]:
        if evaluation.action == "transition_required":
            logger.info(
                "Phase transition required: session=%s phase=%s -> next=%s",
                evaluation.session_id,
                evaluation.phase_name,
                evaluation.next_phase,
            )
            return (
                ModelSessionPhaseCommand(
                    topic=_TOPIC_TRANSITION,
                    command_type=_CMD_TYPE_TRANSITION,
                    payload={
                        "correlation_id": str(evaluation.correlation_id),
                        "session_id": evaluation.session_id,
                        "phase_name": evaluation.phase_name,
                        "transition": "exit",
                        "next_phase": evaluation.next_phase,
                        "reason": evaluation.reason,
                        "elapsed_seconds": evaluation.elapsed_seconds,
                        "cost_usd": evaluation.cost_usd,
                        "budget_usd": evaluation.budget_usd,
                    },
                ),
                "transition_dispatched",
            )

        if evaluation.action == "halt_required":
            logger.warning(
                "Session halt required: session=%s reason=%s",
                evaluation.session_id,
                evaluation.reason,
            )
            return (
                ModelSessionPhaseCommand(
                    topic=_TOPIC_HALT,
                    command_type=_CMD_TYPE_HALT,
                    payload={
                        "correlation_id": str(evaluation.correlation_id),
                        "session_id": evaluation.session_id,
                        "phase_name": evaluation.phase_name,
                        "reason": evaluation.reason,
                        "cost_usd": evaluation.cost_usd,
                    },
                ),
                "halt_dispatched",
            )

        if evaluation.action == "budget_warning":
            # Budget warnings are delegated to the dispatcher — orchestrator only routes
            logger.info(
                "Budget warning for session=%s phase=%s — delegating to dispatcher",
                evaluation.session_id,
                evaluation.phase_name,
            )
            return (
                ModelSessionPhaseCommand(
                    topic=_TOPIC_TRANSITION,
                    command_type=_CMD_TYPE_TRANSITION,
                    payload={
                        "correlation_id": str(evaluation.correlation_id),
                        "session_id": evaluation.session_id,
                        "phase_name": evaluation.phase_name,
                        "transition": "budget_warning",
                        "reason": evaluation.reason,
                        "elapsed_seconds": evaluation.elapsed_seconds,
                        "cost_usd": evaluation.cost_usd,
                        "budget_usd": evaluation.budget_usd,
                    },
                ),
                "warning_delegated",
            )

        # no_action — nothing to emit beyond tick-completed
        return None, "no_action"


__all__ = ["HandlerSessionPhaseOrchestrator"]
