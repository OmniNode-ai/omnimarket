# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_phase_reducer.

Verifies the canonical reducer path from session-start event to materialized
phase projection.

Related:
    - OMN-11230: session phase reducer
    - OMN-13283: canonical node-purity validator rollout
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
)


@pytest.mark.unit
def test_session_phase_reducer_golden_chain_start_projection(tmp_path: Path) -> None:
    """session.started -> reducer output projection + phase_state.yaml."""
    handler = HandlerSessionPhaseReducer()
    state_path = tmp_path / "phase_state.yaml"
    timestamp = datetime(2026, 6, 19, 2, 30, 0, tzinfo=UTC)

    result = handler.handle(
        input_data={
            "event": {
                "event_type": "session.started",
                "session_id": "session-golden-chain",
                "timestamp": timestamp.isoformat(),
                "phase": "health_gate",
                "budget_elapsed_pct": 10,
                "active_worker_count": 2,
            }
        },
        state_path=str(state_path),
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

    state = yaml.safe_load(state_path.read_text())
    assert state["session_id"] == "session-golden-chain"
    assert state["current_phase"] == "health_gate"
    assert state["budget_elapsed_pct"] == 10
