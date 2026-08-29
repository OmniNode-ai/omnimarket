# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase state reducer — pure delta(state, event) -> new_state.

The reducer is the CANONICAL authority for session phase state, and that state
lives in the platform DATABASE. The handler itself performs no I/O: the
runtime's ``state_io`` dispatch seam loads the ``session_id``-keyed row before
``handle()`` and CAS-persists the fold after it, exactly as it does for
delegation workflow state. ``contract.yaml``'s ``state_io`` block declares the
binding, and its ``database`` / ``table`` are ``${env.VAR:default}`` contract
overlay refs, so an operator rebinds the state of record without a code change.

OMN-16924. The previous state of record was a cwd-relative
``.onex_state/session/phase_state.yaml``. The runtime container's cwd is
``/app`` (``root:root 0755``) while the process runs as ``omniinfra``, so every
bus dispatch of this reducer raised ``PermissionError: [Errno 13] Permission
denied: '.onex_state'`` and DLQ'd — a 100% failure rate on all three subscribed
topics, on every lane. Making ``/app`` writable would have been worse, not
better: three runtime containers auto-wire this node per lane and would each
have kept their own divergent copy of "the canonical authority".

Operator ruling, verbatim: *"onex_state should be configurable via contract
overlay right? for our purposes, state should only be kept in the database. if
you disagree, let's have a conversation."*

Related:
    - OMN-11230: Task 6: Create node_session_phase_reducer (REDUCER)
    - OMN-11224: Session phase control loop epic
    - OMN-16790: fold the WIRE payload, not a hand-built local envelope
    - OMN-16924: state of record moves to the database; the file path is gone
    - OMN-14208: the state_io dispatch seam this node now rides
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

EVENT_TYPE_SESSION_STARTED = "session.started"
EVENT_TYPE_SESSION_ENDED = "session.ended"
EVENT_TYPE_SESSION_PHASE_STATE = "session.phase.state"


class ModelSessionPhaseState(BaseModel):
    """Canonical session phase state materialized by this reducer.

    All fields the evaluator and hook need are present here. Persisted by the
    runtime into the contract-declared ``state_io`` table, one row per
    ``session_id`` (OMN-16924).
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
      ``node_session_phase_dispatcher``
      (``contracts/wire/session_phase_state_v1.yaml``, OMN-16955). Required:
      ``session_id``, ``phase_name``, ``transition``, ``correlation_id``,
      ``emitted_at``. There is no ``event_type`` on this wire — the
      dispatcher never sets one; the fold discriminator falls through to
      ``EVENT_TYPE_SESSION_PHASE_STATE`` by elimination (see
      ``resolved_event_type``).

    ``extra="ignore"``: each wire schema carries transport/identity fields this
    reducer does not fold (``entity_id``, ``correlation_id``, ``working_directory``,
    ``git_branch``, ``schema_version``, ...). Ignoring them is the omniclaude event
    convention; forbidding them would DLQ every well-formed message.

    OMN-16955: the dispatcher's wire shape is authoritative — this model
    transcribes ``phase_name`` (not a reducer-invented ``phase``) and carries
    ``transition`` for completeness, since renaming the field at the producer
    would ripple into every other consumer of that wire contract. ``to_event()``
    is where the transcription happens: it prefers an explicit ``phase`` (the
    shape hand-built tests and any future producer that adopts the reducer's
    own vocabulary still use) and falls back to ``phase_name`` (the shape the
    one real producer, ``node_session_phase_dispatcher``, actually emits).

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
    phase_name: str | None = None
    transition: str | None = None
    phase_index: int | None = None
    budget_elapsed_pct: int | None = None
    active_worker_count: int | None = None
    exit_conditions_met: tuple[str, ...] | None = None
    exit_conditions_pending: tuple[str, ...] | None = None
    last_evaluation: str | None = None

    @model_validator(mode="after")
    def _require_an_event_timestamp(self) -> ModelSessionPhaseReducerInput:
        """Fail validation — not dispatch — when no timestamp is on the wire.

        All three wire contracts declare ``emitted_at`` required: the two
        omniclaude hook contracts, and — as of OMN-16955 —
        ``onex.evt.omnimarket.session-phase-state.v1``
        (``node_session_phase_dispatcher``, an EFFECT node, injects it at
        emission time). ``timestamp`` remains accepted for any producer that
        prefers the reducer's own field name; no current producer emits it. A
        message with neither is genuinely malformed and belongs in the DLQ.
        Rejecting it here keeps that decision at the model boundary with a
        readable message instead of surfacing as an ``AttributeError``
        mid-fold.
        """
        if self.emitted_at is None and self.timestamp is None:
            raise ValueError(
                "session phase event carries neither 'emitted_at' (every "
                "current wire producer: the two omniclaude hook contracts and "
                "the omnimarket phase-state event) nor 'timestamp' (accepted "
                "but currently unused by any producer); the reducer cannot "
                "order a fold without one"
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
        """Project the wire payload onto the reducer's internal fold event.

        OMN-16955: ``phase`` wins when a producer supplies it explicitly;
        otherwise ``phase_name`` (the field the actual dispatcher wire
        contract carries) is transcribed onto it. Without this fallback the
        dispatcher's real payload folds with ``phase=None`` and never
        advances ``current_phase`` — the silent half of the original defect.
        """
        return ModelSessionPhaseEvent(
            event_type=self.resolved_event_type,
            session_id=self.session_id,
            timestamp=self.resolved_timestamp,
            phase=self.phase if self.phase is not None else self.phase_name,
            phase_index=self.phase_index,
            budget_elapsed_pct=self.budget_elapsed_pct,
            active_worker_count=self.active_worker_count,
            exit_conditions_met=self.exit_conditions_met,
            exit_conditions_pending=self.exit_conditions_pending,
            last_evaluation=self.last_evaluation,
        )


class HandlerSessionPhaseReducer:
    """Pure reducer: delta(state, event) -> new_state.

    Stateless and deterministic — it performs NO I/O. Prior state arrives from
    the database, put there and read back by the runtime's ``state_io`` dispatch
    seam; the handler reaches it through a request-scoped ContextVar-backed
    proxy (``state_codec.SessionPhaseStateProxy``), which is a memory read, not
    a side effect. ``descriptor.purity: pure`` in ``contract.yaml`` is now
    literally true; before OMN-16924 it was not, because ``handle()`` wrote a
    YAML file.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(self, request: ModelSessionPhaseReducerInput) -> dict[str, Any]:
        """Canonical def-B entrypoint: fold ONE wire event into the phase state.

        The signature is load-bearing (OMN-16790).
        ``handler_wiring._resolve_def_b_input_model_type`` recognises a def-B
        handler only when ``handle`` exposes exactly ONE positional parameter
        annotated with a concrete ``BaseModel``. It then validates the extracted
        domain payload into that model at the adapter boundary.

        OMN-16924: the former keyword-only ``state_path`` parameter is gone, with
        no shim. It was the last reference to the ``.onex_state`` file the
        operator ruling removed, and a keyword default is invisible to bus
        dispatch anyway — every message took the unwritable default.

        Prior state comes from the ``session_id``-keyed row the runtime bound
        before this call, and the folded result is handed back through the same
        proxy for the runtime to CAS-persist after this call returns. Outside a
        state_io dispatch (the CLI, a unit test) the proxy holds nothing: the
        fold sees no prior state and nothing is persisted. That is deliberate —
        a local fallback store is exactly what this ticket deletes.

        The return stays an untyped ``dict``. Under the state_io wiring branch
        ``_normalize_handler_result`` classifies a bare ``BaseModel`` return as
        an output EVENT and a reducer's typed projection return as a
        ``ModelProjectionIntent`` — and an intent with no registered projector
        makes ``DispatchResultApplier`` raise. This node's durable output is its
        row, not an emission, so a dict return is the shape that correctly
        records a no-op-emission fold.
        """
        # Local import: state_codec imports this module's models, so a
        # module-level import here would be circular.
        from omnimarket.nodes.node_session_phase_reducer.state_codec import (
            get_default_proxy,
        )

        proxy = get_default_proxy()
        session_id = request.session_id
        prior = proxy.get(session_id)
        new_state = self.delta(prior, request.to_event())
        proxy[session_id] = new_state
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
