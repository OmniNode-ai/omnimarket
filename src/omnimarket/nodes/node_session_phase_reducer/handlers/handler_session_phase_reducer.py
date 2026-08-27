# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase state reducer — pure delta(state, event) -> new_state.

The reducer is the CANONICAL authority for session phase state.
The hook reads what the reducer writes to .onex_state/session/phase_state.yaml.

Related:
    - OMN-11230: Task 6: Create node_session_phase_reducer (REDUCER)
    - OMN-11224: Session phase control loop epic
    - OMN-16790: fold the WIRE payload, not a hand-built local envelope
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

_DEFAULT_STATE_PATH = ".onex_state/session/phase_state.yaml"

EVENT_TYPE_SESSION_STARTED = "session.started"
EVENT_TYPE_SESSION_ENDED = "session.ended"
EVENT_TYPE_SESSION_PHASE_STATE = "session.phase.state"


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
    """The WIRE payload of ONE subscribed message — this handler's def-B input.

    OMN-16790. The previous shape was ``{"state": ..., "event": ...}``: a LOCAL
    invocation envelope that appears in NO wire schema for ANY of the three topics
    ``contract.yaml`` subscribes to. Every message on the bus therefore raised
    ``KeyError: 'event'`` and DLQ'd — a 100% failure rate from the day the
    subscription existed. The wire schemas this model is transcribed from:

    * ``onex.evt.omniclaude.session-started.v1`` — omniclaude
      ``src/omniclaude/hooks/contracts/wire/session_started_v1.yaml``. Required:
      ``entity_id``, ``session_id``, ``correlation_id``, ``causation_id``,
      ``emitted_at``, ``working_directory``, ``hook_source``.
    * ``onex.evt.omniclaude.session-ended.v1`` — omniclaude
      ``src/omniclaude/hooks/contracts/wire/session_ended_v1.yaml``. Required:
      the same identity/timing fields plus ``reason``.
    * ``onex.evt.omnimarket.session-phase-state.v1`` — published by
      ``node_session_phase_dispatcher``; carries the phase fields and an explicit
      ``event_type``.

    ``extra="ignore"``: each wire schema carries transport/identity fields this
    reducer does not fold (``entity_id``, ``correlation_id``, ``working_directory``,
    ``git_branch``, ``schema_version``, ...). Ignoring them is the omniclaude event
    convention; forbidding them would DLQ every well-formed message.

    ``resolved_event_type`` keys on fields the wire contracts declare REQUIRED for
    exactly one topic each (``hook_source`` on started, ``reason`` on ended), so
    the discriminator is contract-grounded rather than conventional. An explicit
    ``event_type`` always wins.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str
    event_type: str | None = None
    emitted_at: datetime | None = None
    timestamp: datetime | None = None
    hook_source: str | None = None
    reason: str | None = None
    phase: str | None = None
    phase_index: int | None = None
    budget_elapsed_pct: int | None = None
    active_worker_count: int | None = None
    exit_conditions_met: tuple[str, ...] | None = None
    exit_conditions_pending: tuple[str, ...] | None = None
    last_evaluation: str | None = None

    @model_validator(mode="after")
    def _require_an_event_timestamp(self) -> ModelSessionPhaseReducerInput:
        """Fail validation — not dispatch — when no timestamp is on the wire.

        Both omniclaude wire contracts declare ``emitted_at`` required and the
        omnimarket phase-state event carries ``timestamp``; a message with
        neither is genuinely malformed and belongs in the DLQ. Rejecting it here
        keeps that decision at the model boundary with a readable message
        instead of surfacing as an ``AttributeError`` mid-fold.
        """
        if self.emitted_at is None and self.timestamp is None:
            raise ValueError(
                "session phase event carries neither 'emitted_at' (omniclaude "
                "hook wire contract) nor 'timestamp' (omnimarket phase-state "
                "event); the reducer cannot order a fold without one"
            )
        return self

    @property
    def resolved_event_type(self) -> str:
        """The fold discriminator, from the message alone.

        A def-B handler receives the validated domain payload and never the
        topic: ``_extract_dispatch_payload`` unwraps the materialized envelope
        down to the payload and discards ``__debug_trace.topic``. The
        discriminator must therefore live in the payload.
        """
        if self.event_type is not None:
            return self.event_type
        if self.hook_source is not None:
            return EVENT_TYPE_SESSION_STARTED
        if self.reason is not None:
            return EVENT_TYPE_SESSION_ENDED
        return EVENT_TYPE_SESSION_PHASE_STATE

    @property
    def resolved_timestamp(self) -> datetime:
        """``timestamp`` when present, else the hook's ``emitted_at``."""
        stamp = self.timestamp if self.timestamp is not None else self.emitted_at
        if stamp is None:  # pragma: no cover — barred by the model validator
            raise ValueError("session phase event has no timestamp")
        return stamp

    def to_event(self) -> ModelSessionPhaseEvent:
        """Project the wire payload onto the reducer's internal fold event."""
        return ModelSessionPhaseEvent(
            event_type=self.resolved_event_type,
            session_id=self.session_id,
            timestamp=self.resolved_timestamp,
            phase=self.phase,
            phase_index=self.phase_index,
            budget_elapsed_pct=self.budget_elapsed_pct,
            active_worker_count=self.active_worker_count,
            exit_conditions_met=self.exit_conditions_met,
            exit_conditions_pending=self.exit_conditions_pending,
            last_evaluation=self.last_evaluation,
        )


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
        request: ModelSessionPhaseReducerInput,
        *,
        state_path: str = _DEFAULT_STATE_PATH,
    ) -> dict[str, Any]:
        """Canonical def-B entrypoint: fold ONE wire event into the phase state.

        The signature is load-bearing (OMN-16790).
        ``handler_wiring._resolve_def_b_input_model_type`` recognises a def-B
        handler only when ``handle`` exposes exactly ONE positional parameter
        annotated with a concrete ``BaseModel``. It then validates the extracted
        domain payload into that model at the adapter boundary. The previous
        signature had TWO positional parameters and a ``dict`` annotation, so the
        resolver returned ``None`` and the runtime passed the raw materialized
        wire dict straight through. ``state_path`` is keyword-only for exactly
        this reason — keyword-only parameters are excluded from the arity check,
        so the CLI keeps its override without breaking bus dispatch.
        """
        path = Path(state_path)
        new_state = self.delta(self._read_state(path), request.to_event())
        self._write_state(new_state, path)
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

    def _read_state(self, path: Path) -> ModelSessionPhaseState | None:
        """Load the prior state this reducer itself materialized.

        A bus message carries ONE event and never the prior state — no wire
        schema on any subscribed topic declares a ``state`` field. The projection
        file IS the reducer's state of record (the module docstring: "the
        reducer is the CANONICAL authority for session phase state"), so it is
        also where a fold reads from.

        A missing file is the legitimate ``idle`` state and yields ``None``. A
        file that exists but does not parse is a real defect and raises — the
        only writer is ``_write_state``, so corruption is never expected traffic.
        """
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text())  # node-purity-ok: OMN-9048
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError(
                f"phase_state.yaml at {path} is not a mapping (got {type(raw).__name__})"
            )
        return ModelSessionPhaseState(**raw)

    def _write_state(self, state: ModelSessionPhaseState, path: Path) -> None:
        """Write phase state to YAML — the reducer's projection side effect."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = state.model_dump(mode="json")
        with path.open("w") as fh:  # node-purity-ok: OMN-9048
            yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=True)
        logger.debug("phase_state.yaml written: %s", path)
