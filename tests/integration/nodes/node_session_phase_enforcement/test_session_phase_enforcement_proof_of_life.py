# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof-of-life integration test for the session phase enforcement pipeline.

Exercises the full end-to-end chain with real models and real durable state,
wiring handler outputs directly (no Kafka) to simulate the event bus:

    evaluator -> orchestrator -> dispatcher -> reducer -> session_phase_state row
                                                              |
                                                    enforcement directive

Contract loaded, tick fires, evaluator runs, reducer folds state, the directive
builder renders it — all paths exercised with real model instances.

OMN-16924: the chain used to terminate in ``.onex_state/session/phase_state.yaml``.
That file is gone — the reducer's state of record is now a session_id-keyed
database row the runtime loads and persists around ``handle()``, and the handler
performs no I/O. ``state_io_dispatch`` plays the runtime's part here.

RESIDUAL, stated rather than papered over: omniclaude's
``session_phase_enforcement`` hook still reads that local file. It reads nothing
today either — the reducer's bus dispatch has never once succeeded (KeyError
before OMN-16790, PermissionError after), so the file was never written on any
lane. Re-pointing the hook at the projection is separate follow-up work; this
test proves the enforcement rendering against the state the platform actually
holds.

[OMN-11283] [OMN-11234] [OMN-16924]
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

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
    ModelSessionPhaseReducerInput,
)
from tests.session_phase_state_io_harness import StateIoRowStore, state_io_dispatch

_SESSION_ID = "sess-omn-11283-proof-of-life"
_PHASE_1_NAME = "phase_1"
_PHASE_2_NAME = "phase_2"


def _build_enforcement_directive(data: dict[str, Any] | None) -> str:
    """Replicate hook logic over the reducer's materialized phase state.

    Takes the state itself rather than a file path — OMN-16924 moved the state of
    record into the database, so "what the hook renders" is a question about the
    projection, not about a file on the developer's disk.
    """
    if not data:
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
def test_session_phase_enforcement_proof_of_life() -> None:
    """Full chain: budget exhausted -> transition_required -> durable row -> directive."""
    store = StateIoRowStore()

    # Step 1: Contract — 2 phases, phase 1 budget = 1 minute.
    # (contract is encoded in the evaluation request; no external YAML needed)
    phase_1_budget_minutes = 1

    # Step 2: Initialize reducer state at phase 1 with started_at = 2 minutes ago.
    started_at = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 21, 9, 2, 0, tzinfo=UTC)  # 2 minutes later

    reducer = HandlerSessionPhaseReducer()
    with state_io_dispatch(store, _SESSION_ID):
        reducer.handle(
            ModelSessionPhaseReducerInput(
                event_type="session.started",
                session_id=_SESSION_ID,
                timestamp=started_at,
                phase=_PHASE_1_NAME,
                phase_index=0,
            )
        )
    assert store.load(_SESSION_ID) is not None, (
        "Reducer must persist a durable row on session.started"
    )

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
                    next_phase=transition_payload["next_phase"],
                    reason=transition_payload["reason"],
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

    # Step 6: Feed phase-state event to reducer -> advance the durable row to phase 2.
    # OMN-16790: the wire event alone. OMN-16924: prior state comes from the row
    # the runtime loaded, which is where its state of record lives.
    with state_io_dispatch(store, _SESSION_ID):
        reducer.handle(
            ModelSessionPhaseReducerInput.model_validate(phase_state_payload)
        )

    # Step 7: Read the durable row — verify the real dispatcher event advanced it.
    persisted = reducer_state = store.load(_SESSION_ID)
    assert persisted is not None, "Reducer must persist a row on phase transition"
    assert persisted.phase_index == 1, (
        f"Expected phase_index=1, got {persisted.phase_index}"
    )
    assert persisted.current_phase == _PHASE_2_NAME, (
        f"Expected current_phase={_PHASE_2_NAME}, got {persisted.current_phase}"
    )
    assert persisted.last_evaluation == "transition_required"

    # Step 8: Verify the enforcement directive renders from that state.
    directive = _build_enforcement_directive(reducer_state.model_dump(mode="json"))
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
def test_budget_exhaustion_triggers_phase_transition() -> None:
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
def test_reducer_state_reflects_transition_at_every_step() -> None:
    """The durable row is accurate after each event: started -> phase_1 -> phase_2."""
    store = StateIoRowStore()
    reducer = HandlerSessionPhaseReducer()
    ts_start = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    ts_tick = datetime(2026, 5, 21, 9, 2, 0, tzinfo=UTC)

    # After session.started
    with state_io_dispatch(store, _SESSION_ID):
        reducer.handle(
            ModelSessionPhaseReducerInput(
                event_type="session.started",
                session_id=_SESSION_ID,
                timestamp=ts_start,
                phase=_PHASE_1_NAME,
                phase_index=0,
            )
        )
    state_1 = store.load(_SESSION_ID)
    assert state_1 is not None
    assert state_1.current_phase == _PHASE_1_NAME
    assert state_1.phase_index == 0

    # After transition to phase_2.
    # OMN-16790: no prior state is passed — a bus message never carries one.
    # OMN-16924: the fold picks the session up from the durable row above, which
    # the runtime loads and rebinds before every dispatch.
    with state_io_dispatch(store, _SESSION_ID):
        reducer.handle(
            ModelSessionPhaseReducerInput(
                event_type="session.phase.state",
                session_id=_SESSION_ID,
                timestamp=ts_tick,
                phase=_PHASE_2_NAME,
                phase_index=1,
                last_evaluation="transition_required",
                budget_elapsed_pct=100,
            )
        )
    state_2 = store.load(_SESSION_ID)
    assert state_2 is not None
    assert state_2.current_phase == _PHASE_2_NAME
    assert state_2.phase_index == 1
    assert state_2.last_evaluation == "transition_required"


def _phase_state(*, evaluation: str, budget_elapsed_pct: int) -> dict[str, Any]:
    """The materialized phase state a directive is rendered from."""
    return {
        "session_id": _SESSION_ID,
        "current_phase": _PHASE_1_NAME,
        "phase_index": 0,
        "budget_elapsed_pct": budget_elapsed_pct,
        "last_evaluation": evaluation,
        "last_tick_at": None,
        "phase_started_at": None,
        "active_worker_count": 0,
        "exit_conditions_met": [],
        "exit_conditions_pending": [],
    }


@pytest.mark.integration
def test_directive_rendering_covers_every_evaluation_outcome() -> None:
    """Each evaluation outcome renders its own enforcement directive.

    OMN-16924: the cases are driven from the materialized state itself rather
    than from a YAML file on disk. The rendering logic under test is unchanged;
    only its input source moved, because the state of record did.
    """
    assert "PHASE ENFORCEMENT" in _build_enforcement_directive(
        _phase_state(evaluation="transition_required", budget_elapsed_pct=100)
    )
    assert "SESSION HALT" in _build_enforcement_directive(
        _phase_state(evaluation="halt_required", budget_elapsed_pct=120)
    )
    assert "PHASE WARNING" in _build_enforcement_directive(
        _phase_state(evaluation="budget_warning", budget_elapsed_pct=85)
    )
    assert (
        _build_enforcement_directive(
            _phase_state(evaluation="no_action", budget_elapsed_pct=40)
        )
        == ""
    )
    assert _build_enforcement_directive(None) == "", (
        "no materialized state must render no directive, not a crash"
    )
