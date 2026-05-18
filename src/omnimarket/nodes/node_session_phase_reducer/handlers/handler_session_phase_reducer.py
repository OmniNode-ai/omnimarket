# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase state reducer — pure delta(state, event) -> new_state.

The reducer is the CANONICAL authority for session phase state.
The hook reads what the reducer writes to .onex_state/session/phase_state.yaml.

Related:
    - OMN-11230: Task 6: Create node_session_phase_reducer (REDUCER)
    - OMN-11224: Session phase control loop epic
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

_DEFAULT_STATE_PATH = ".onex_state/session/phase_state.yaml"


class ModelSessionPhaseState(BaseModel):
    """Canonical session phase state materialized by this reducer.

    All fields the evaluator and hook need are present here.
    Written to .onex_state/session/phase_state.yaml after every event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    current_phase: str
    phase_index: int = 0
    phase_started_at: datetime | None = None
    budget_elapsed_pct: int = 0
    active_worker_count: int = 0
    exit_conditions_met: tuple[str, ...] = ()
    exit_conditions_pending: tuple[str, ...] = ()
    last_evaluation: str = "no_action"
    last_tick_at: datetime | None = None


class ModelSessionPhaseEvent(BaseModel):
    """Incoming event that drives a phase state transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    session_id: str
    timestamp: datetime
    phase: str | None = None
    phase_index: int | None = None
    budget_elapsed_pct: int | None = None
    active_worker_count: int | None = None
    exit_conditions_met: tuple[str, ...] | None = None
    exit_conditions_pending: tuple[str, ...] | None = None
    last_evaluation: str | None = None


class ModelSessionPhaseReducerInput(BaseModel):
    """Handler input envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelSessionPhaseState | None = None
    event: ModelSessionPhaseEvent


class HandlerSessionPhaseReducer:
    """Pure reducer: delta(state, event) -> new_state.

    The ONE allowed side effect for a reducer: writing the projection file
    (.onex_state/session/phase_state.yaml). The hook reads this file.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(
        self,
        input_data: dict[str, Any],
        state_path: str = _DEFAULT_STATE_PATH,
    ) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim."""
        state_data = input_data.get("state")
        event_data = input_data["event"]
        state = ModelSessionPhaseState(**state_data) if state_data else None
        event = ModelSessionPhaseEvent(**event_data)
        new_state = self.delta(state, event)
        self._write_state(new_state, Path(state_path))
        return {
            "projections": [new_state.model_dump(mode="json")],
        }

    def delta(
        self,
        state: ModelSessionPhaseState | None,
        event: ModelSessionPhaseEvent,
    ) -> ModelSessionPhaseState:
        """Compute the next state from current state (or None) + event.

        Handles three event types:
          - session-started: initialize fresh state
          - session-ended: mark session as ended
          - session-phase-state: apply partial update to existing state
        """
        event_type = event.event_type

        if event_type == "session.started":
            return ModelSessionPhaseState(
                session_id=event.session_id,
                current_phase=event.phase or "start",
                phase_index=event.phase_index or 0,
                phase_started_at=event.timestamp,
                budget_elapsed_pct=event.budget_elapsed_pct or 0,
                active_worker_count=event.active_worker_count or 0,
                exit_conditions_met=event.exit_conditions_met or (),
                exit_conditions_pending=event.exit_conditions_pending or (),
                last_evaluation=event.last_evaluation or "no_action",
                last_tick_at=event.timestamp,
            )

        if state is None:
            logger.warning(
                "Received %s with no prior state — ignoring (session not started)",
                event_type,
            )
            return ModelSessionPhaseState(
                session_id=event.session_id,
                current_phase="unknown",
                last_tick_at=event.timestamp,
            )

        if event.session_id != state.session_id:
            logger.warning(
                "Rejecting event: session_id mismatch (event=%s, state=%s)",
                event.session_id,
                state.session_id,
            )
            return state

        if event_type == "session.ended":
            return state.model_copy(
                update={
                    "current_phase": "ended",
                    "last_tick_at": event.timestamp,
                }
            )

        # session-phase-state: apply partial update — only update provided fields
        updates: dict[str, Any] = {"last_tick_at": event.timestamp}

        if event.phase is not None:
            if event.phase != state.current_phase:
                updates["current_phase"] = event.phase
                updates["phase_started_at"] = event.timestamp
                updates["phase_index"] = event.phase_index or 0
            elif event.phase_index is not None:
                updates["phase_index"] = event.phase_index

        if event.budget_elapsed_pct is not None:
            updates["budget_elapsed_pct"] = event.budget_elapsed_pct
        if event.active_worker_count is not None:
            updates["active_worker_count"] = event.active_worker_count
        if event.exit_conditions_met is not None:
            updates["exit_conditions_met"] = event.exit_conditions_met
        if event.exit_conditions_pending is not None:
            updates["exit_conditions_pending"] = event.exit_conditions_pending
        if event.last_evaluation is not None:
            updates["last_evaluation"] = event.last_evaluation

        logger.debug(
            "Session phase state updated: session=%s phase=%s index=%d",
            state.session_id,
            updates.get("current_phase", state.current_phase),
            updates.get("phase_index", state.phase_index),
        )

        return state.model_copy(update=updates)

    def _write_state(self, state: ModelSessionPhaseState, path: Path) -> None:
        """Write phase state to YAML — the reducer's projection side effect."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = state.model_dump(mode="json")
        with path.open("w") as fh:
            yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=True)
        logger.debug("phase_state.yaml written: %s", path)
