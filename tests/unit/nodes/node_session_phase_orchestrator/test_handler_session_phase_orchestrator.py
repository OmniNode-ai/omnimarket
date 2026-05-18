# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerSessionPhaseOrchestrator.

Four cases from the ticket spec:
  1. transition_required → emits transition command event
  2. halt_required → emits halt command event
  3. no_action / budget_warning → no events emitted
  4. ORCHESTRATOR contract: result must never be set in output
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
)

_TOPIC_TRANSITION = "onex.cmd.omnimarket.session-phase-transition.v1"
_TOPIC_HALT = "onex.cmd.omnimarket.session-halt-required.v1"


def _make_input(
    *,
    action: str,
    session_id: str = "sess-test",
    phase_name: str = "merge",
    next_phase: str | None = "closeout",
    budget_elapsed_pct: int = 50,
    correlation_id: str = "corr-abc",
    reason: str = "test reason",
) -> dict[str, object]:
    return {
        "action": action,
        "session_id": session_id,
        "phase_name": phase_name,
        "next_phase": next_phase,
        "budget_elapsed_pct": budget_elapsed_pct,
        "correlation_id": correlation_id,
        "reason": reason,
    }


@pytest.mark.unit
class TestOrchestratorEmitsTransitionOnEvaluation:
    def test_orchestrator_emits_transition_on_evaluation(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        output = handler.handle(_make_input(action="transition_required"))

        assert "events" in output
        assert "result" not in output
        events = output["events"]
        assert len(events) == 1
        evt = events[0]
        assert evt["topic"] == _TOPIC_TRANSITION
        assert evt["payload"]["session_id"] == "sess-test"
        assert evt["payload"]["phase_name"] == "merge"
        assert evt["payload"]["next_phase"] == "closeout"
        assert evt["payload"]["reason"] == "test reason"


@pytest.mark.unit
class TestOrchestratorEmitsHaltOnHaltRequired:
    def test_orchestrator_emits_halt_on_halt_required(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        output = handler.handle(
            _make_input(
                action="halt_required", budget_elapsed_pct=105, reason="budget exceeded"
            )
        )

        assert "events" in output
        assert "result" not in output
        events = output["events"]
        assert len(events) == 1
        evt = events[0]
        assert evt["topic"] == _TOPIC_HALT
        assert evt["payload"]["session_id"] == "sess-test"
        assert evt["payload"]["phase_name"] == "merge"
        assert evt["payload"]["budget_elapsed_pct"] == 105
        assert evt["payload"]["reason"] == "budget exceeded"


@pytest.mark.unit
class TestOrchestratorNoActionOnInBudget:
    def test_orchestrator_no_action_on_in_budget(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        output = handler.handle(_make_input(action="no_action", budget_elapsed_pct=30))

        assert "events" in output
        assert "result" not in output
        assert output["events"] == []

    def test_orchestrator_no_action_on_budget_warning(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        output = handler.handle(
            _make_input(action="budget_warning", budget_elapsed_pct=82)
        )

        assert "events" in output
        assert "result" not in output
        assert output["events"] == []


@pytest.mark.unit
class TestOrchestratorNeverReturnsResult:
    def test_orchestrator_never_returns_result(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()

        for action in (
            "no_action",
            "budget_warning",
            "transition_required",
            "halt_required",
        ):
            output = handler.handle(_make_input(action=action))
            assert "result" not in output, f"result must not be set for action={action}"

    def test_orchestrator_guard_raises_on_result_injection(self) -> None:
        handler = HandlerSessionPhaseOrchestrator()
        with pytest.raises(ValueError, match="ORCHESTRATOR contract violation"):
            handler._guard_no_result({"result": "some_value", "events": []})
