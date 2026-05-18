# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_session_phase_reducer.

TDD: tests written before implementation was complete. Each test asserts the
pure delta function and the YAML projection side effect.

Related:
    - OMN-11230: Task 6: Create node_session_phase_reducer (REDUCER)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
    ModelSessionPhaseEvent,
    ModelSessionPhaseState,
)

_SESSION_ID = "sess-2026-05-18-001"
_NOW = datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def _started_event(**kwargs: object) -> ModelSessionPhaseEvent:
    return ModelSessionPhaseEvent(
        event_type="session.started",
        session_id=_SESSION_ID,
        timestamp=_NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _phase_event(phase: str, **kwargs: object) -> ModelSessionPhaseEvent:
    return ModelSessionPhaseEvent(
        event_type="session.phase.state",
        session_id=_SESSION_ID,
        timestamp=_LATER,
        phase=phase,
        **kwargs,  # type: ignore[arg-type]
    )


def _initial_state() -> ModelSessionPhaseState:
    return ModelSessionPhaseState(
        session_id=_SESSION_ID,
        current_phase="start",
        phase_index=0,
        phase_started_at=_NOW,
        last_tick_at=_NOW,
    )


@pytest.mark.unit
class TestSessionPhaseReducerDelta:
    def test_reducer_initializes_on_session_start(self) -> None:
        """SESSION_STARTED -> phase_state with phase='start', index=0."""
        handler = HandlerSessionPhaseReducer()
        event = _started_event()

        new_state = handler.delta(None, event)

        assert new_state.session_id == _SESSION_ID
        assert new_state.current_phase == "start"
        assert new_state.phase_index == 0
        assert new_state.phase_started_at == _NOW
        assert new_state.last_tick_at == _NOW

    def test_reducer_initializes_with_explicit_phase(self) -> None:
        """SESSION_STARTED with explicit phase -> that phase is captured."""
        handler = HandlerSessionPhaseReducer()
        event = _started_event(phase="health_gate", phase_index=1)

        new_state = handler.delta(None, event)

        assert new_state.current_phase == "health_gate"
        assert new_state.phase_index == 1

    def test_reducer_advances_phase_on_transition(self) -> None:
        """Phase transition event -> phase_index incremented, phase_started_at reset."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event("rsd_scoring", phase_index=1)

        new_state = handler.delta(state, event)

        assert new_state.current_phase == "rsd_scoring"
        assert new_state.phase_index == 1
        assert new_state.phase_started_at == _LATER

    def test_reducer_no_phase_change_preserves_started_at(self) -> None:
        """Same phase update (e.g. tick) does not reset phase_started_at."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event("start", budget_elapsed_pct=25)

        new_state = handler.delta(state, event)

        assert new_state.current_phase == "start"
        assert new_state.phase_started_at == _NOW  # unchanged
        assert new_state.budget_elapsed_pct == 25

    def test_reducer_records_worker_count(self) -> None:
        """Worker count update -> active_worker_count reflected in new state."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event("dispatch", active_worker_count=3)

        new_state = handler.delta(state, event)

        assert new_state.active_worker_count == 3

    def test_reducer_records_exit_conditions(self) -> None:
        """Exit condition update -> conditions reflected in new state."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event(
            "start",
            exit_conditions_met=("budget_exhausted",),
            exit_conditions_pending=("worker_idle",),
        )

        new_state = handler.delta(state, event)

        assert "budget_exhausted" in new_state.exit_conditions_met
        assert "worker_idle" in new_state.exit_conditions_pending

    def test_reducer_records_last_evaluation(self) -> None:
        """Evaluation result -> last_evaluation field updated."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event("start", last_evaluation="advance_phase")

        new_state = handler.delta(state, event)

        assert new_state.last_evaluation == "advance_phase"

    def test_reducer_handles_session_ended(self) -> None:
        """SESSION_ENDED -> phase set to 'ended'."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = ModelSessionPhaseEvent(
            event_type="session.ended",
            session_id=_SESSION_ID,
            timestamp=_LATER,
        )

        new_state = handler.delta(state, event)

        assert new_state.current_phase == "ended"
        assert new_state.last_tick_at == _LATER

    def test_reducer_state_is_immutable(self) -> None:
        """delta() returns a new object — original state is unchanged (frozen model)."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = _phase_event("rsd_scoring", phase_index=1)

        new_state = handler.delta(state, event)

        assert state.current_phase == "start"
        assert new_state.current_phase == "rsd_scoring"

    def test_reducer_rejects_non_start_event_with_no_state(self) -> None:
        """Non-start event with state=None returns unknown placeholder, not crashing."""
        handler = HandlerSessionPhaseReducer()
        event = _phase_event("rsd_scoring", phase_index=1)

        new_state = handler.delta(None, event)

        assert new_state.current_phase == "unknown"

    def test_reducer_rejects_mismatched_session_id(self) -> None:
        """Event for a different session_id is ignored — state unchanged."""
        handler = HandlerSessionPhaseReducer()
        state = _initial_state()
        event = ModelSessionPhaseEvent(
            event_type="session.phase.state",
            session_id="sess-DIFFERENT",
            timestamp=_LATER,
            phase="rsd_scoring",
        )

        new_state = handler.delta(state, event)

        assert new_state is state


@pytest.mark.unit
class TestSessionPhaseReducerWritesStateFile:
    def test_reducer_writes_state_file(self, tmp_path: Path) -> None:
        """After handle(), phase_state.yaml is written to the specified path."""
        handler = HandlerSessionPhaseReducer()
        state_file = tmp_path / "phase_state.yaml"

        handler.handle(
            input_data={
                "event": {
                    "event_type": "session.started",
                    "session_id": _SESSION_ID,
                    "timestamp": _NOW.isoformat(),
                }
            },
            state_path=str(state_file),
        )

        assert state_file.exists()
        data = yaml.safe_load(state_file.read_text())
        assert data["session_id"] == _SESSION_ID
        assert data["current_phase"] == "start"

    def test_state_file_contains_all_required_fields(self, tmp_path: Path) -> None:
        """YAML file contains every field the evaluator and hook need."""
        handler = HandlerSessionPhaseReducer()
        state_file = tmp_path / "phase_state.yaml"

        handler.handle(
            input_data={
                "event": {
                    "event_type": "session.started",
                    "session_id": _SESSION_ID,
                    "timestamp": _NOW.isoformat(),
                    "phase": "health_gate",
                    "budget_elapsed_pct": 10,
                    "active_worker_count": 2,
                }
            },
            state_path=str(state_file),
        )

        data = yaml.safe_load(state_file.read_text())
        required_fields = {
            "session_id",
            "current_phase",
            "phase_index",
            "budget_elapsed_pct",
            "active_worker_count",
            "exit_conditions_met",
            "exit_conditions_pending",
            "last_evaluation",
        }
        assert required_fields.issubset(data.keys())

    def test_state_file_is_overwritten_on_second_event(self, tmp_path: Path) -> None:
        """Each event overwrites the previous state file (latest-state-wins)."""
        handler = HandlerSessionPhaseReducer()
        state_file = tmp_path / "phase_state.yaml"

        # First event: session started
        result = handler.handle(
            input_data={
                "event": {
                    "event_type": "session.started",
                    "session_id": _SESSION_ID,
                    "timestamp": _NOW.isoformat(),
                }
            },
            state_path=str(state_file),
        )

        current_state = result["projections"][0]

        # Second event: phase transition
        handler.handle(
            input_data={
                "state": current_state,
                "event": {
                    "event_type": "session.phase.state",
                    "session_id": _SESSION_ID,
                    "timestamp": _LATER.isoformat(),
                    "phase": "rsd_scoring",
                    "phase_index": 1,
                },
            },
            state_path=str(state_file),
        )

        data = yaml.safe_load(state_file.read_text())
        assert data["current_phase"] == "rsd_scoring"
        assert data["phase_index"] == 1

    def test_state_file_parent_dirs_created(self, tmp_path: Path) -> None:
        """Nested path is created if it doesn't exist."""
        handler = HandlerSessionPhaseReducer()
        state_file = tmp_path / "nested" / "deep" / "phase_state.yaml"

        handler.handle(
            input_data={
                "event": {
                    "event_type": "session.started",
                    "session_id": _SESSION_ID,
                    "timestamp": _NOW.isoformat(),
                }
            },
            state_path=str(state_file),
        )

        assert state_file.exists()

    def test_handle_returns_projections_only(self, tmp_path: Path) -> None:
        """REDUCER nodes return projections[] only — no events, intents, or result."""
        handler = HandlerSessionPhaseReducer()
        state_file = tmp_path / "phase_state.yaml"

        result = handler.handle(
            input_data={
                "event": {
                    "event_type": "session.started",
                    "session_id": _SESSION_ID,
                    "timestamp": _NOW.isoformat(),
                }
            },
            state_path=str(state_file),
        )

        assert set(result.keys()) == {"projections"}
        assert len(result["projections"]) == 1
