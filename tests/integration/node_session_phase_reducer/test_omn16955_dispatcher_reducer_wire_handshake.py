# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16955: dispatcher phase-state payload must validate and fold."""

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
    monkeypatch.chdir(tmp_path)

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

    phase_state_payload = _actual_dispatcher_phase_state_payload(
        phase_name="phase_2", transition="enter"
    )

    result, error = await _dispatch_to_reducer(phase_state_payload)

    assert error is None, (
        f"the real dispatch seam raised {type(error).__name__}: {error} -- the "
        "dispatcher's actual payload did not validate against the reducer's "
        "declared input model"
    )
    assert result is not None, "dispatch produced no ModelDispatchResult"

    try:
        from omnimarket.nodes.node_session_phase_reducer.state_codec import (
            get_default_proxy,
        )
    except ModuleNotFoundError:
        state_file = tmp_path / ".onex_state" / "session" / "phase_state.yaml"
        assert state_file.exists(), "the reducer's projection side effect did not fire"
        state: Any = yaml.safe_load(state_file.read_text())
        assert state["session_id"] == _SESSION_ID
        current_phase = state["current_phase"]
    else:
        state = get_default_proxy().get(_SESSION_ID)
        assert state is not None, "the reducer did not update the state proxy"
        assert state.session_id == _SESSION_ID
        current_phase = state.current_phase

    assert current_phase == "phase_2", (
        "the fold did not advance current_phase -- phase_name was not "
        f"transcribed onto the reducer's phase field (state={state!r})"
    )
