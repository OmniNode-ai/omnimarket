# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_session_phase_reducer (OMN-13674).

REDUCER archetype -> Variant B: the pure ``delta(state, event) -> new_state``
fold is registered on the canonical in-memory bus (``EventBusInmemory`` via
``integration_event_bus``) through ``LocalRuntimeBusAdapter``
(``drive_round_trip``). One reduce envelope (prior state + incoming event) is
published on the reducer subscribe topic and the materialized
``ModelSessionPhaseState`` projection republished on the reduced-state topic is
asserted.

The contract's ``state_machine`` declares three states — ``idle`` (no session),
``active`` (session materialized), ``ended`` (terminal) — with transitions
``idle --session_started--> active``, ``active --session_phase_state-->
active``, ``active --session_ended--> ended``. This suite folds every declared
event type and asserts the projection state each fold lands in:

  * ``idle -> active``  : ``session.started`` materialises a fresh projection,
  * ``active -> active``: a ``session-phase-state`` event folds a partial update
    (phase advance, budget/worker/condition fields) onto the existing state,
  * ``active -> ended`` : ``session.ended`` marks ``current_phase == "ended"``,
  * IDEMPOTENCY         : re-folding the same ``session.started`` is a
    deterministic re-init, and an out-of-order event for a different session_id
    is rejected (state returned unchanged), and
  * NEGATIVE CONTROL    : a non-start event with NO prior state (the ``idle``
    reject edge) yields the sentinel ``current_phase == "unknown"`` rather than
    fabricating an active session.

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
    ModelSessionPhaseEvent,
    ModelSessionPhaseReducerInput,
    ModelSessionPhaseState,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.evt.omnimarket.session-phase-reduce.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.session-phase-state-reduced.v1"

_SESSION = "sess-reducer"
_T0 = datetime(2026, 6, 19, 2, 30, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 19, 2, 45, 0, tzinfo=UTC)


class _ReducerBusHandler:
    """Bus-facing shim: reconstructs (state, event) from the envelope and returns
    the ``delta`` projection so the adapter republishes it to the output topic.

    ``delta`` is the pure reduce core (no file I/O); the projection-file write
    side effect is covered separately by the unit suite.
    """

    def __init__(self) -> None:
        self._handler = HandlerSessionPhaseReducer()

    async def handle(self, **payload: Any) -> ModelSessionPhaseState:
        envelope = ModelSessionPhaseReducerInput.model_validate(payload)
        return self._handler.delta(envelope.state, envelope.event)


def _started_event(
    *, phase: str = "health_gate", budget: int = 10, workers: int = 2
) -> ModelSessionPhaseEvent:
    return ModelSessionPhaseEvent(
        event_type="session.started",
        session_id=_SESSION,
        timestamp=_T0,
        phase=phase,
        budget_elapsed_pct=budget,
        active_worker_count=workers,
    )


def _active_state(*, phase: str = "health_gate") -> ModelSessionPhaseState:
    return ModelSessionPhaseState(
        session_id=_SESSION,
        current_phase=phase,
        phase_started_at=_T0,
        budget_elapsed_pct=10,
        active_worker_count=2,
        last_tick_at=_T0,
    )


async def _projection(
    envelope: ModelSessionPhaseReducerInput, bus: Any, *, group: str
) -> dict[str, Any]:
    history = await drive_round_trip(
        bus,
        handler=_ReducerBusHandler(),
        handler_name="session-phase-reducer",
        input_model_cls=None,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=envelope.model_dump_json().encode("utf-8"),
        group_id=group,
    )
    assert len(history) == 1, "expected exactly one reduced projection"
    projection: dict[str, Any] = json.loads(history[0].value)
    return projection


# ---------------------------------------------------------------------------
# Declared FSM state coverage — every event type folded, projection asserted.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestReducerDeclaredStateCoverage:
    async def test_idle_to_active_on_session_started(
        self, integration_event_bus: Any
    ) -> None:
        """idle -> active: session.started materialises a fresh projection."""
        envelope = ModelSessionPhaseReducerInput(state=None, event=_started_event())
        projection = await _projection(
            envelope, integration_event_bus, group="reducer-start"
        )
        assert projection["session_id"] == _SESSION
        assert projection["current_phase"] == "health_gate"
        assert projection["budget_elapsed_pct"] == 10
        assert projection["active_worker_count"] == 2

    async def test_active_to_active_on_phase_state_fold(
        self, integration_event_bus: Any
    ) -> None:
        """active -> active: a phase-state event folds a partial update."""
        event = ModelSessionPhaseEvent(
            event_type="session.phase_state",
            session_id=_SESSION,
            timestamp=_T1,
            phase="merge",
            phase_index=3,
            budget_elapsed_pct=55,
            active_worker_count=4,
            exit_conditions_met=("pr_merged",),
            exit_conditions_pending=("ci_green",),
            last_evaluation="budget_warning",
        )
        envelope = ModelSessionPhaseReducerInput(state=_active_state(), event=event)
        projection = await _projection(
            envelope, integration_event_bus, group="reducer-fold"
        )
        assert projection["current_phase"] == "merge"
        assert projection["phase_index"] == 3
        assert projection["budget_elapsed_pct"] == 55
        assert projection["active_worker_count"] == 4
        assert projection["exit_conditions_met"] == ["pr_merged"]
        assert projection["exit_conditions_pending"] == ["ci_green"]
        assert projection["last_evaluation"] == "budget_warning"

    async def test_active_to_ended_on_session_ended(
        self, integration_event_bus: Any
    ) -> None:
        """active -> ended (terminal): session.ended marks current_phase ended."""
        event = ModelSessionPhaseEvent(
            event_type="session.ended",
            session_id=_SESSION,
            timestamp=_T1,
        )
        envelope = ModelSessionPhaseReducerInput(state=_active_state(), event=event)
        projection = await _projection(
            envelope, integration_event_bus, group="reducer-end"
        )
        assert projection["current_phase"] == "ended"
        assert projection["session_id"] == _SESSION


# ---------------------------------------------------------------------------
# Idempotency / out-of-order + negative control (idle reject).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestReducerDimensions:
    async def test_reapplying_session_started_is_deterministic(
        self, integration_event_bus: Any
    ) -> None:
        """Re-folding the same session.started yields an identical projection."""
        envelope = ModelSessionPhaseReducerInput(state=None, event=_started_event())
        first = await _projection(
            envelope, integration_event_bus, group="reducer-idem-1"
        )
        second = await _projection(
            envelope, integration_event_bus, group="reducer-idem-2"
        )
        assert first == second

    async def test_out_of_order_event_for_other_session_is_rejected(
        self, integration_event_bus: Any
    ) -> None:
        """An event whose session_id != the state's session_id leaves state as-is."""
        foreign = ModelSessionPhaseEvent(
            event_type="session.phase_state",
            session_id="sess-OTHER",
            timestamp=_T1,
            phase="merge",
            budget_elapsed_pct=99,
        )
        envelope = ModelSessionPhaseReducerInput(
            state=_active_state(phase="health_gate"), event=foreign
        )
        projection = await _projection(
            envelope, integration_event_bus, group="reducer-mismatch"
        )
        # Unchanged: the foreign event did not overwrite phase or budget.
        assert projection["current_phase"] == "health_gate"
        assert projection["budget_elapsed_pct"] == 10
        assert projection["session_id"] == _SESSION

    async def test_idle_reject_non_start_event_yields_unknown(
        self, integration_event_bus: Any
    ) -> None:
        """NEGATIVE CONTROL: a non-start event with NO prior state (idle) must
        yield the sentinel ``current_phase == "unknown"`` rather than fabricate
        an active session."""
        event = ModelSessionPhaseEvent(
            event_type="session.phase_state",
            session_id=_SESSION,
            timestamp=_T1,
            phase="merge",
            budget_elapsed_pct=50,
        )
        envelope = ModelSessionPhaseReducerInput(state=None, event=event)
        projection = await _projection(
            envelope, integration_event_bus, group="reducer-idle-reject"
        )
        assert projection["current_phase"] == "unknown"
        assert projection["session_id"] == _SESSION
