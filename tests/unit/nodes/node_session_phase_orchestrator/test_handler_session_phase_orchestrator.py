# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerSessionPhaseOrchestrator.

Four cases from the ticket spec:
  1. Evaluation = transition_required -> publishes SESSION_PHASE_TRANSITION command
  2. Evaluation = halt_required -> publishes SESSION_HALT_REQUIRED command
  3. Evaluation = no_action -> no transition/halt commands, only tick-completed
  4. Orchestrator result has no `result` field (never returns payload result)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
)
from omnimarket.nodes.node_session_phase_orchestrator.models.model_orchestrator_input import (
    ModelSessionPhaseEvaluation,
    ModelSessionPhaseOrchestratorInput,
)

_TOPIC_TRANSITION = "onex.cmd.omnimarket.session-phase-transition.v1"
_TOPIC_HALT = "onex.cmd.omnimarket.session-halt-required.v1"
_TOPIC_TICK_COMPLETED = "onex.evt.omnimarket.session-orchestrator-tick-completed.v1"
_CMD_TYPE_TRANSITION = "omnimarket.session-phase-transition"
_CMD_TYPE_HALT = "omnimarket.session-halt-required"
_CMD_TYPE_TICK_COMPLETED = "omnimarket.session-orchestrator-tick-completed"


def _make_evaluation(
    *,
    action: str = "no_action",
    next_phase: str | None = None,
    reason: str = "test reason",
    cost_usd: float = 1.0,
    budget_usd: float = 5.0,
) -> ModelSessionPhaseEvaluation:
    return ModelSessionPhaseEvaluation(
        correlation_id=uuid4(),
        session_id="sess-2026-05-18-test",
        phase_name="merge",
        action=action,  # type: ignore[arg-type]
        reason=reason,
        next_phase=next_phase,
        elapsed_seconds=60.0,
        cost_usd=cost_usd,
        budget_usd=budget_usd,
    )


@pytest.mark.unit
class TestOrchestratorEmitsTransition:
    def test_emits_transition_on_transition_required(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(
            action="transition_required", next_phase="integration"
        )
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        transition_cmds = [
            c for c in result.commands if c.command_type == _CMD_TYPE_TRANSITION
        ]
        assert len(transition_cmds) == 1
        cmd = transition_cmds[0]
        assert cmd.topic == _TOPIC_TRANSITION
        assert cmd.payload["session_id"] == "sess-2026-05-18-test"
        assert cmd.payload["phase_name"] == "merge"
        assert cmd.payload["transition"] == "exit"
        assert cmd.payload["next_phase"] == "integration"
        assert result.tick_outcome == "transition_dispatched"

    def test_tick_completed_always_emitted(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(
            action="transition_required", next_phase="integration"
        )
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        tick_cmds = [
            c for c in result.commands if c.command_type == _CMD_TYPE_TICK_COMPLETED
        ]
        assert len(tick_cmds) == 1
        assert tick_cmds[0].topic == _TOPIC_TICK_COMPLETED


@pytest.mark.unit
class TestOrchestratorEmitsHalt:
    def test_emits_halt_on_halt_required(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(
            action="halt_required", reason="cost ceiling breached"
        )
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        halt_cmds = [c for c in result.commands if c.command_type == _CMD_TYPE_HALT]
        assert len(halt_cmds) == 1
        cmd = halt_cmds[0]
        assert cmd.topic == _TOPIC_HALT
        assert cmd.payload["session_id"] == "sess-2026-05-18-test"
        assert cmd.payload["reason"] == "cost ceiling breached"
        assert result.tick_outcome == "halt_dispatched"

    def test_halt_takes_priority_over_transition(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluations = (
            _make_evaluation(action="transition_required", next_phase="integration"),
            _make_evaluation(action="halt_required", reason="critical failure"),
        )
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=evaluations)
        )

        assert result.tick_outcome == "halt_dispatched"
        halt_cmds = [c for c in result.commands if c.command_type == _CMD_TYPE_HALT]
        assert len(halt_cmds) == 1


@pytest.mark.unit
class TestOrchestratorNoAction:
    def test_no_transition_or_halt_on_no_action(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(action="no_action")
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        transition_cmds = [
            c for c in result.commands if c.command_type == _CMD_TYPE_TRANSITION
        ]
        halt_cmds = [c for c in result.commands if c.command_type == _CMD_TYPE_HALT]
        assert len(transition_cmds) == 0
        assert len(halt_cmds) == 0
        assert result.tick_outcome == "no_action"

    def test_tick_completed_emitted_even_on_no_action(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(action="no_action")
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        tick_cmds = [
            c for c in result.commands if c.command_type == _CMD_TYPE_TICK_COMPLETED
        ]
        assert len(tick_cmds) == 1
        assert tick_cmds[0].payload["tick_outcome"] == "no_action"


@pytest.mark.unit
class TestOrchestratorNeverReturnsResult:
    def test_result_has_no_result_field(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(action="no_action")
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        assert not hasattr(result, "result"), (
            "Orchestrator must never return a 'result' field — it emits commands only"
        )

    def test_result_fields_are_commands_and_outcome(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        evaluation = _make_evaluation(
            action="transition_required", next_phase="closeout"
        )
        result = handler.handle(
            ModelSessionPhaseOrchestratorInput(evaluations=(evaluation,))
        )

        assert hasattr(result, "commands")
        assert hasattr(result, "tick_outcome")
        assert hasattr(result, "correlation_id")
