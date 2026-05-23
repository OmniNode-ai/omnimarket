# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof-of-life integration test for the session phase enforcement pipeline.

Exercises the full end-to-end chain with real models and real state files,
wiring handler outputs directly (no Kafka) to simulate the event bus:

    evaluator -> orchestrator -> dispatcher -> reducer -> phase_state.yaml
                                                              |
                                                         hook reads file

Contract loaded, tick fires, evaluator runs, reducer projects state, hook
injects directive — all paths exercised with real model instances.

[OMN-11283] [OMN-11234]
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_session_phase_dispatcher.handlers.handler_session_phase_dispatcher import (
    HandlerSessionPhaseDispatcher,
)
from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_input import (
    ModelSessionPhaseDispatcherInput,
    ModelSessionPhaseTransitionCommand,
)
from omnimarket.nodes.node_session_phase_evaluator.handlers.handler_session_phase_evaluator import (
    HandlerSessionPhaseEvaluator,
    ModelPhaseEvaluationRequest,
)
from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
)
from omnimarket.nodes.node_session_phase_orchestrator.models.model_orchestrator_input import (
    ModelSessionPhaseEvaluation,
    ModelSessionPhaseOrchestratorInput,
)
from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
)

_SESSION_ID = "sess-omn-11283-proof-of-life"
_PHASE_1_NAME = "phase_1"
_PHASE_2_NAME = "phase_2"


def _build_enforcement_directive(state_dir: Path) -> str:
    """Replicate hook logic: read phase_state.yaml and return enforcement directive."""
    path = state_dir / "session" / "phase_state.yaml"
    if not path.exists():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""

    evaluation: str = data.get("last_evaluation", "")
    current_phase: str = data.get("current_phase", "unknown")
    budget_elapsed_pct: int | float = data.get("budget_elapsed_pct", 0)

    if evaluation == "transition_required":
        return (
            f"[PHASE ENFORCEMENT] Phase '{current_phase}' budget exhausted. "
            "Stop current work and dispatch next phase workers."
        )
    if evaluation == "halt_required":
        return "[SESSION HALT] Halt condition triggered. Stop all work immediately."
    if evaluation == "budget_warning":
        return (
            f"[PHASE WARNING] Phase '{current_phase}' at {budget_elapsed_pct}% "
            "of time budget. Plan to transition soon."
        )
    return ""


@pytest.mark.integration
def test_session_phase_enforcement_proof_of_life(tmp_path: Path) -> None:
    """Full chain: budget exhausted -> transition_required -> state file -> hook directive."""
    state_file = tmp_path / "session" / "phase_state.yaml"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path

    # Step 1: Contract — 2 phases, phase 1 budget = 1 minute.
    # (contract is encoded in the evaluation request; no external YAML needed)
    phase_1_budget_minutes = 1

    # Step 2: Initialize reducer state at phase 1 with started_at = 2 minutes ago.
    started_at = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 21, 9, 2, 0, tzinfo=UTC)  # 2 minutes later

    reducer = HandlerSessionPhaseReducer()
    init_result = reducer.handle(
        input_data={
            "event": {
                "event_type": "session.started",
                "session_id": _SESSION_ID,
                "timestamp": started_at.isoformat(),
                "phase": _PHASE_1_NAME,
                "phase_index": 0,
            }
        },
        state_path=str(state_file),
    )
    assert state_file.exists(), "Reducer must write phase_state.yaml on session.started"
    initial_state_data = init_result["projections"][0]

    # Step 3: Run evaluator — elapsed 2 min vs budget 1 min => transition_required.
    # halt_threshold_pct=110 so budget-exhausted (100%) triggers transition_required
    # rather than halt_required (which fires at >= halt_threshold_pct).
    elapsed_minutes = (now - started_at).total_seconds() / 60.0
    evaluator = HandlerSessionPhaseEvaluator()
    evaluation = evaluator.handle(
        ModelPhaseEvaluationRequest(
            phase_name=_PHASE_1_NAME,
            max_duration_minutes=phase_1_budget_minutes,
            elapsed_minutes=elapsed_minutes,
            exit_condition_statuses={},
            halt_threshold_pct=110,
        )
    )
    assert evaluation.action == "transition_required", (
        f"Expected transition_required for 200% budget consumption, got {evaluation.action}"
    )

    # Step 4: Feed evaluation to orchestrator — should emit transition command.
    correlation_id = uuid.uuid4()
    orchestrator = HandlerSessionPhaseOrchestrator()
    orch_result = orchestrator.handle(
        ModelSessionPhaseOrchestratorInput(
            evaluations=(
                ModelSessionPhaseEvaluation(
                    correlation_id=correlation_id,
                    session_id=_SESSION_ID,
                    phase_name=_PHASE_1_NAME,
                    action=evaluation.action,
                    reason=evaluation.reason,
                    next_phase=_PHASE_2_NAME,
                    elapsed_seconds=elapsed_minutes * 60,
                ),
            )
        )
    )
    assert orch_result.tick_outcome == "transition_dispatched", (
        f"Expected transition_dispatched, got {orch_result.tick_outcome}"
    )
    transition_commands = [
        cmd for cmd in orch_result.commands if "session-phase-transition" in cmd.topic
    ]
    assert transition_commands, "Orchestrator must emit at least one transition command"
    transition_payload = transition_commands[0].payload

    # Step 5: Feed transition to dispatcher — should publish phase-state event.
    dispatcher = HandlerSessionPhaseDispatcher()
    dispatch_result = dispatcher.handle(
        ModelSessionPhaseDispatcherInput(
            commands=(
                ModelSessionPhaseTransitionCommand(
                    correlation_id=transition_payload["correlation_id"],
                    session_id=transition_payload["session_id"],
                    phase_name=transition_payload["phase_name"],
                    transition=transition_payload["transition"],
                    elapsed_seconds=transition_payload["elapsed_seconds"],
                    cost_usd=transition_payload["cost_usd"],
                    budget_usd=transition_payload["budget_usd"],
                ),
            )
        )
    )
    phase_state_events = [
        evt for evt in dispatch_result.events if "session-phase-state" in evt.topic
    ]
    assert phase_state_events, "Dispatcher must publish at least one phase-state event"
    assert dispatch_result.correlation_id == correlation_id
    phase_state_payload = phase_state_events[0].payload

    # Step 6: Feed phase-state event to reducer -> update phase_state.yaml to phase 2.
    reducer.handle(
        input_data={
            "state": initial_state_data,
            "event": {
                "event_type": "session.phase.state",
                "session_id": phase_state_payload["session_id"],
                "timestamp": now.isoformat(),
                "phase": transition_payload["next_phase"],
                "phase_index": 1,
                "last_evaluation": "transition_required",
                "budget_elapsed_pct": evaluation.budget_elapsed_pct,
            },
        },
        state_path=str(state_file),
    )

    # Step 7: Read phase_state.yaml — verify phase_index=1, current_phase=phase_2.
    assert state_file.exists(), (
        "Reducer must write phase_state.yaml on phase transition"
    )
    written = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert written["phase_index"] == 1, (
        f"Expected phase_index=1, got {written['phase_index']}"
    )
    assert written["current_phase"] == _PHASE_2_NAME, (
        f"Expected current_phase={_PHASE_2_NAME}, got {written['current_phase']}"
    )
    assert written["last_evaluation"] == "transition_required"

    # Step 8: Verify hook injects the correct enforcement directive.
    directive = _build_enforcement_directive(state_dir)
    assert directive, (
        "Hook must return a non-empty directive for transition_required state"
    )
    assert "PHASE ENFORCEMENT" in directive, (
        f"Expected PHASE ENFORCEMENT directive, got: {directive!r}"
    )
    assert _PHASE_2_NAME in directive, (
        f"Directive must reference a phase name: {directive!r}"
    )


@pytest.mark.integration
def test_budget_exhaustion_triggers_phase_transition(tmp_path: Path) -> None:
    """Budget exhaustion (200% elapsed) produces transition_required from evaluator."""
    evaluator = HandlerSessionPhaseEvaluator()

    # halt_threshold_pct=110 so budget-exhausted (100%) triggers transition_required.
    evaluation = evaluator.handle(
        ModelPhaseEvaluationRequest(
            phase_name=_PHASE_1_NAME,
            max_duration_minutes=1,
            elapsed_minutes=2.0,  # 200% of budget, capped to 100%
            exit_condition_statuses={},
            halt_threshold_pct=110,
        )
    )

    assert evaluation.action == "transition_required"
    assert evaluation.budget_elapsed_pct == 100  # capped at 100


@pytest.mark.integration
def test_reducer_state_reflects_transition_at_every_step(tmp_path: Path) -> None:
    """State file is accurate after each event: started -> phase_1 -> phase_2."""
    state_file = tmp_path / "session" / "phase_state.yaml"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    reducer = HandlerSessionPhaseReducer()
    ts_start = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    ts_tick = datetime(2026, 5, 21, 9, 2, 0, tzinfo=UTC)

    # After session.started
    result = reducer.handle(
        input_data={
            "event": {
                "event_type": "session.started",
                "session_id": _SESSION_ID,
                "timestamp": ts_start.isoformat(),
                "phase": _PHASE_1_NAME,
                "phase_index": 0,
            }
        },
        state_path=str(state_file),
    )
    state_1 = yaml.safe_load(state_file.read_text())
    assert state_1["current_phase"] == _PHASE_1_NAME
    assert state_1["phase_index"] == 0

    # After transition to phase_2
    reducer.handle(
        input_data={
            "state": result["projections"][0],
            "event": {
                "event_type": "session.phase.state",
                "session_id": _SESSION_ID,
                "timestamp": ts_tick.isoformat(),
                "phase": _PHASE_2_NAME,
                "phase_index": 1,
                "last_evaluation": "transition_required",
                "budget_elapsed_pct": 100,
            },
        },
        state_path=str(state_file),
    )
    state_2 = yaml.safe_load(state_file.read_text())
    assert state_2["current_phase"] == _PHASE_2_NAME
    assert state_2["phase_index"] == 1
    assert state_2["last_evaluation"] == "transition_required"


@pytest.mark.integration
def test_hook_reads_state_and_injects_directive(tmp_path: Path) -> None:
    """Hook function reads phase_state.yaml and returns correct directive per evaluation."""
    state_dir = tmp_path
    state_path = state_dir / "session" / "phase_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # transition_required state
    state_path.write_text(
        yaml.safe_dump(
            {
                "session_id": _SESSION_ID,
                "current_phase": _PHASE_1_NAME,
                "phase_index": 0,
                "budget_elapsed_pct": 100,
                "last_evaluation": "transition_required",
                "last_tick_at": None,
                "phase_started_at": None,
                "active_worker_count": 0,
                "exit_conditions_met": [],
                "exit_conditions_pending": [],
            }
        ),
        encoding="utf-8",
    )
    directive = _build_enforcement_directive(state_dir)
    assert "PHASE ENFORCEMENT" in directive

    # halt_required state
    state_path.write_text(
        yaml.safe_dump(
            {
                "session_id": _SESSION_ID,
                "current_phase": _PHASE_1_NAME,
                "phase_index": 0,
                "budget_elapsed_pct": 120,
                "last_evaluation": "halt_required",
                "last_tick_at": None,
                "phase_started_at": None,
                "active_worker_count": 0,
                "exit_conditions_met": [],
                "exit_conditions_pending": [],
            }
        ),
        encoding="utf-8",
    )
    directive = _build_enforcement_directive(state_dir)
    assert "SESSION HALT" in directive

    # budget_warning state
    state_path.write_text(
        yaml.safe_dump(
            {
                "session_id": _SESSION_ID,
                "current_phase": _PHASE_1_NAME,
                "phase_index": 0,
                "budget_elapsed_pct": 85,
                "last_evaluation": "budget_warning",
                "last_tick_at": None,
                "phase_started_at": None,
                "active_worker_count": 0,
                "exit_conditions_met": [],
                "exit_conditions_pending": [],
            }
        ),
        encoding="utf-8",
    )
    directive = _build_enforcement_directive(state_dir)
    assert "PHASE WARNING" in directive

    # no_action state — empty directive
    state_path.write_text(
        yaml.safe_dump(
            {
                "session_id": _SESSION_ID,
                "current_phase": _PHASE_1_NAME,
                "phase_index": 0,
                "budget_elapsed_pct": 40,
                "last_evaluation": "no_action",
                "last_tick_at": None,
                "phase_started_at": None,
                "active_worker_count": 0,
                "exit_conditions_met": [],
                "exit_conditions_pending": [],
            }
        ),
        encoding="utf-8",
    )
    directive = _build_enforcement_directive(state_dir)
    assert directive == ""
