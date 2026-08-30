# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_phase_reducer.

Verifies the canonical reducer path from session-start event to materialized
phase projection.

OMN-16790: this suite used to hand-build ``{"event": {...}}`` — a LOCAL
invocation envelope carried by no wire schema on any subscribed topic — so it
stayed green through the entire live outage in which every real message raised
``KeyError: 'event'``. It now drives the canonical def-B signature with the
wire payload shape a producer actually emits.

Related:
    - OMN-11230: session phase reducer
    - OMN-13283: canonical node-purity validator rollout
    - OMN-16790: fold the WIRE payload, not a hand-built local envelope
    - OMN-16924: the fold's durable output is a database row, not a YAML file
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
    ModelSessionPhaseReducerInput,
)
from tests.session_phase_state_io_harness import StateIoRowStore, state_io_dispatch


@pytest.mark.unit
def test_session_phase_reducer_golden_chain_start_projection() -> None:
    """session.started -> reducer projection + the durable session_phase_state row.

    OMN-16924: the chain's terminal artifact is the database row the runtime
    persists after handle(), not a YAML file. ``state_io_dispatch`` plays the
    runtime's part around the fold.
    """
    handler = HandlerSessionPhaseReducer()
    store = StateIoRowStore()
    session_id = "session-golden-chain"
    timestamp = datetime(2026, 6, 19, 2, 30, 0, tzinfo=UTC)

    with state_io_dispatch(store, session_id):
        result = handler.handle(
            ModelSessionPhaseReducerInput(
                event_type="session.started",
                session_id=session_id,
                timestamp=timestamp,
                phase="health_gate",
                budget_elapsed_pct=10,
                active_worker_count=2,
            )
        )
    serialized_timestamp = "2026-06-19T02:30:00Z"

    assert result["projections"] == [
        {
            "session_id": "session-golden-chain",
            "current_phase": "health_gate",
            "phase_index": 0,
            "phase_started_at": serialized_timestamp,
            "budget_elapsed_pct": 10,
            "active_worker_count": 2,
            "exit_conditions_met": [],
            "exit_conditions_pending": [],
            "last_evaluation": "no_action",
            "last_tick_at": serialized_timestamp,
        }
    ]

    persisted = store.load(session_id)
    assert persisted is not None, "the golden chain persisted no durable row"
    assert persisted.session_id == session_id
    assert persisted.current_phase == "health_gate"
    assert persisted.budget_elapsed_pct == 10
