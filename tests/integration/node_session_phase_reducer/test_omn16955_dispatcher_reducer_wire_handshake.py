# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16955 — the dispatcher's phase-state payload must validate AND fold.

THE DEFECT (two independent breaks on the reducer's third subscribed topic,
``onex.evt.omnimarket.session-phase-state.v1``):

1. ``HandlerSessionPhaseDispatcher`` published ``session_id, phase_name,
   transition, correlation_id, elapsed_seconds, cost_usd`` — no ``emitted_at``
   and no ``timestamp``. ``ModelSessionPhaseReducerInput._require_an_event_
   timestamp`` raises when both are ``None``, so every dispatcher-produced
   phase-state message failed validation at the adapter boundary and DLQ'd.
2. The dispatcher emits ``phase_name``; the reducer read ``phase``. Under
   ``extra="ignore"`` this was silent — ``phase`` resolved to ``None``, so
   even a validated message would not advance ``current_phase``.

This test drives the REAL dispatch seam (``_make_dispatch_callback``, the
same seam OMN-16790's ``test_omn16790_wire_dispatch.py`` uses) with the
dispatcher's ACTUAL emitted payload — built by ``HandlerSessionPhaseDispatcher``
itself, never hand-injected — closing the CodeRabbit gap on omnimarket#2204:
"This test injects both values and can pass without proving the
dispatcher-to-reducer contract."

RED before the fix: the dispatcher payload carries neither ``emitted_at`` nor
``timestamp``, so the dispatch raises a ``pydantic.ValidationError`` at the
adapter boundary with the ``_require_an_event_timestamp`` message.

Related:
    - OMN-16955: this ticket
    - OMN-16790: same seam, the two omniclaude hook topics
    - OMN-16924: state-of-record move (DB), independent of this defect
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums import EnumNodeKind
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from pydantic import ValidationError

from omnimarket.nodes.node_session_phase_dispatcher.handlers.handler_session_phase_dispatcher import (
    HandlerSessionPhaseDispatcher,
)
from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_input import (
    ModelSessionPhaseDispatcherInput,
    ModelSessionPhaseTransitionCommand,
)
from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
    ModelSessionPhaseReducerInput,
)

_TOPIC_PHASE_STATE = "onex.evt.omnimarket.session-phase-state.v1"
_SESSION_ID = "sess-omn-16955-wire-handshake"


def _materialized_wire_envelope(topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The exact dict ``MessageDispatchEngine`` hands a dispatcher.

    Reproduced structurally per ``test_omn16790_wire_dispatch.py`` —
    ``_materialize_envelope_with_bindings`` always builds ``payload`` +
    ``__bindings`` + ``__debug_trace`` and nothing else.
    """
    return {
        "payload": payload,
        "__bindings": {},
        "__debug_trace": {
            "topic": topic,
            "correlation_id": "a1c2e3f4-5b6d-47a8-9c0e-1f2a3b4c5d6e",
        },
    }


async def _dispatch_to_reducer(
    payload: dict[str, Any],
) -> tuple[Any, BaseException | None]:
    """Drive the real reducer dispatch callback with a phase-state payload."""
    callback = _make_dispatch_callback(
        HandlerSessionPhaseReducer(),  # type: ignore[arg-type]
        None,
        EnumNodeKind.REDUCER,
        None,
    )
    try:
        result = await callback(  # type: ignore[arg-type]
            _materialized_wire_envelope(_TOPIC_PHASE_STATE, payload)
        )
    except Exception as exc:
        return None, exc
    return result, None


def _actual_dispatcher_phase_state_payload(
    *, phase_name: str, transition: str = "enter"
) -> dict[str, Any]:
    """Build the dispatcher's REAL published payload — not a hand-built stand-in.

    Runs ``HandlerSessionPhaseDispatcher.handle()`` for real and extracts the
    phase-state event it publishes, exactly what a Kafka consumer receives.
    """
    dispatcher = HandlerSessionPhaseDispatcher()
    result = dispatcher.handle(
        ModelSessionPhaseDispatcherInput(
            commands=(
                ModelSessionPhaseTransitionCommand(
                    correlation_id=uuid.uuid4(),
                    session_id=_SESSION_ID,
                    phase_name=phase_name,
                    transition=transition,  # type: ignore[arg-type]
                    elapsed_seconds=12.0,
                    cost_usd=0.5,
                ),
            )
        )
    )
    phase_state_events = [
        evt for evt in result.events if _TOPIC_PHASE_STATE in evt.topic
    ]
    assert phase_state_events, "dispatcher must publish a phase-state event"
    return phase_state_events[0].payload


@pytest.mark.unit
def test_old_shape_without_a_timestamp_is_rejected_by_the_real_adapter_check() -> None:
    """Regression lock: the pre-fix shape must still fail validation.

    Asserts the rejection against the REAL adapter check
    (``ModelSessionPhaseReducerInput._require_an_event_timestamp``), not a
    hand-rolled substitute — this is the exact shape
    ``HandlerSessionPhaseDispatcher`` emitted before OMN-16955.
    """
    old_shape = {
        "session_id": _SESSION_ID,
        "phase_name": "phase_1",
        "transition": "enter",
        "correlation_id": str(uuid.uuid4()),
        "elapsed_seconds": 12.0,
        "cost_usd": 0.5,
    }

    with pytest.raises(ValidationError, match="neither 'emitted_at'"):
        ModelSessionPhaseReducerInput(**old_shape)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatcher_actual_payload_validates_and_advances_current_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's ACTUAL published payload must fold and advance phase.

    RED before OMN-16955: dispatch raises ``ValidationError`` (no timestamp on
    the wire). GREEN after: the message validates, and because the wire's
    ``phase_name`` is transcribed onto the reducer's ``phase``, the fold
    genuinely advances ``current_phase`` — the CodeRabbit gap this test closes.
    """
    monkeypatch.chdir(tmp_path)

    # Seed prior state: session already in phase_1 (session.started fold).
    started_result, started_error = await _dispatch_to_reducer(
        {
            "session_id": _SESSION_ID,
            "event_type": "session.started",
            "emitted_at": "2026-08-29T09:00:00+00:00",
            "phase": "phase_1",
            "phase_index": 0,
        }
    )
    assert started_error is None, (
        f"seeding session.started raised {type(started_error).__name__}: "
        f"{started_error}"
    )
    assert started_result is not None

    # The dispatcher's REAL emitted payload for an "enter phase_2" transition.
    phase_state_payload = _actual_dispatcher_phase_state_payload(
        phase_name="phase_2", transition="enter"
    )

    result, error = await _dispatch_to_reducer(phase_state_payload)

    assert error is None, (
        f"the real dispatch seam raised {type(error).__name__}: {error} — the "
        "dispatcher's actual payload did not validate against the reducer's "
        "declared input model"
    )
    assert result is not None, "dispatch produced no ModelDispatchResult"

    state_file = tmp_path / ".onex_state" / "session" / "phase_state.yaml"
    assert state_file.exists(), "the reducer's projection side effect did not fire"
    state = yaml.safe_load(state_file.read_text())
    assert state["session_id"] == _SESSION_ID
    assert state["current_phase"] == "phase_2", (
        "the fold did not advance current_phase — phase_name was not "
        f"transcribed onto the reducer's phase field (state={state!r})"
    )
