# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16790 — the reducer must fold the WIRE payload, not a hand-built envelope.

THE DEFECT. ``HandlerSessionPhaseReducer.handle`` indexed ``input_data["event"]``
-- a key that appears in NO wire schema for ANY of the three topics the contract
subscribes to. ``{"state": ..., "event": ...}`` is a LOCAL invocation envelope
(the shape ``__main__.py`` builds); the bus never carries it.

The handler also failed the def-B arity/annotation rule in
``omnibase_infra.runtime.auto_wiring.handler_wiring._resolve_def_b_input_model_type``:
two positional parameters (``input_data``, ``state_path``) and a ``dict[str, Any]``
annotation, so the resolver returned ``None`` and ``_make_dispatch_callback`` fell
through to ``dispatch_arg = envelope`` -- the raw materialized wire dict
``{"payload": ..., "__bindings": ..., "__debug_trace": ...}``. Indexing ``"event"``
on THAT is the live ``KeyError: 'event'``.

Live on the .201 stability-test lane, 2026-08-27T19:58:38Z::

    [ERROR] omnibase_infra.runtime.service_kernel: Dispatcher
      'dispatcher.auto.node_session_phase_reducer.HandlerSessionPhaseReducer.
       reduce_session_phase_f81977b1' failed: KeyError: 'event'
    [ERROR] handler_wiring: Auto-wiring callback error:
      topic=onex.evt.omniclaude.session-started.v1
      error_type=HandlerDispatchFailureError
    [WARNING] mixin_kafka_dlq: Raw message published to DLQ
    [ERROR] handler_wiring: metric_name=boundary_swallow_prevented dlq_routed=true

1666 occurrences in 5000 log lines. Because ``node_dlq_replay_effect`` replays the
DLQ back onto the source topic, every failure re-armed itself: the replayed
message failed again, DLQ'd again, and was replayed again -- ``x-original-dlq-offset``
had reached 50,555,610 on a single 2026-08-18 test session-start. That unbounded
loop, not the handler cost, is what drove host load to ~14.

WHY THIS IS THE REAL SEAM. Every existing test for this node calls
``handler.handle(input_data={"event": {...}}, state_path=...)`` directly -- it
builds by hand the exact envelope the bus never produces, so it passed throughout
the outage. This test drives the REAL ``_make_dispatch_callback`` from the
installed ``omnibase_infra`` with the REAL materialized wire envelope and a
verbatim payload captured off the stability lane's Redpanda.

Related:
    - OMN-16790: this ticket
    - OMN-16767: same defect FAMILY (raw dict reaching a typed handler), different
      handler and different arm -- ``omnibase_infra#2937`` fixed the ``db_io``
      arm-selection bug and does NOT reach this node, which declares no ``db_io``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums import EnumNodeKind
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
)

_TOPIC_SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"
_TOPIC_SESSION_ENDED = "onex.evt.omniclaude.session-ended.v1"

# Captured verbatim, 2026-08-27, from the stability lane:
#   docker exec omnibase-infra-stability-test-redpanda \
#     rpk topic consume onex.evt.omniclaude.session-started.v1 -n 2 -o -2
# This is the byte-for-byte payload that produced KeyError: 'event'.
_LIVE_SESSION_STARTED_PAYLOAD: dict[str, Any] = json.loads(
    '{"session_id": "omn16162-live-proof-1787064555", '
    '"working_directory": "omniclaude", '
    '"hook_source": "startup", '
    '"git_branch": "jonah/omn-16162-s0-wire-sessionstart-sessionend-hooks", '
    '"correlation_id": "omn16162-live-proof-1787064555", '
    '"causation_id": null, '
    '"emitted_at": "2026-08-18T14:49:20.803648+00:00", '
    '"entity_id": "d4b1fa8e-999d-db51-4f7e-ca7fb670e659", '
    '"schema_version": "1.0.0"}'
)

# Shape declared by omniclaude's wire contract
# src/omniclaude/hooks/contracts/wire/session_ended_v1.yaml (``reason`` required).
_SESSION_ENDED_PAYLOAD: dict[str, Any] = {
    "session_id": "omn16162-live-proof-1787064555",
    "reason": "exit",
    "correlation_id": "omn16162-live-proof-1787064555",
    "causation_id": None,
    "emitted_at": "2026-08-18T15:49:20.803648+00:00",
    "entity_id": "d4b1fa8e-999d-db51-4f7e-ca7fb670e659",
    "duration_seconds": 3600.0,
    "tools_used_count": 12,
    "schema_version": "1.0.0",
}


def _materialized_wire_envelope(topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The exact dict ``MessageDispatchEngine`` hands a dispatcher.

    ``_materialize_envelope_with_bindings`` always builds ``payload`` +
    ``__bindings`` + ``__debug_trace`` and nothing else. Reproduced structurally
    here because ``omnimarket`` drives the dispatch callback directly rather than
    booting the whole engine.
    """
    return {
        "payload": payload,
        "__bindings": {},
        "__debug_trace": {
            "topic": topic,
            "correlation_id": "63861990-f8a6-4a7c-ac73-fd6f2d25f335",
        },
    }


async def _dispatch(
    topic: str, payload: dict[str, Any]
) -> tuple[Any, BaseException | None]:
    """Drive the real dispatch callback, returning ``(result, raised_or_None)``.

    The exception is CAPTURED rather than allowed to propagate so that a broken
    seam fails these tests on their own ``assert`` — a bare ``KeyError`` escaping
    the test body is a ``RED_EXCEPTION``, which the OMN-15340 parity gate treats
    as no evidence at all (a test that errors out cannot be shown to discriminate
    the flip; see ``scripts/ci/parity_replay.py``). Asserting on the captured
    outcome also states the expectation in the failure message instead of leaving
    a reader to infer it from a traceback.
    """
    callback = _make_dispatch_callback(
        HandlerSessionPhaseReducer(),  # type: ignore[arg-type]
        None,
        EnumNodeKind.REDUCER,
        None,
    )
    try:
        result = await callback(_materialized_wire_envelope(topic, payload))  # type: ignore[arg-type]
    except Exception as exc:
        # The captured outcome IS the assertion subject; see the docstring.
        return None, exc
    return result, None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_session_started_payload_dispatches_without_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verbatim stability-lane payload must fold, not raise KeyError: 'event'.

    RED before the fix: the dispatch raises ``KeyError: 'event'`` from
    ``handler_session_phase_reducer.py`` line 100, which this test reports as a
    failed assertion on the captured outcome.
    """
    monkeypatch.chdir(tmp_path)

    result, error = await _dispatch(
        _TOPIC_SESSION_STARTED, _LIVE_SESSION_STARTED_PAYLOAD
    )

    assert error is None, (
        f"the real dispatch seam raised {type(error).__name__}: {error} — the "
        "runtime handed the handler something other than its declared input model"
    )
    assert result is not None, "dispatch produced no ModelDispatchResult"

    state_file = tmp_path / ".onex_state" / "session" / "phase_state.yaml"
    assert state_file.exists(), "the reducer's projection side effect did not fire"
    state = yaml.safe_load(state_file.read_text())
    assert state["session_id"] == "omn16162-live-proof-1787064555"
    assert state["current_phase"] == "start"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_started_then_ended_folds_across_two_wire_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prior state comes from the projection file, not from the message.

    A bus message carries ONE event and never the prior state -- there is no
    ``state`` key on any wire schema. The reducer must therefore read the state it
    itself materialized. Without that read, ``session.ended`` would hit the
    ``state is None`` branch of ``delta`` and clobber a live session's phase with
    ``"unknown"`` on every event after the first.
    """
    monkeypatch.chdir(tmp_path)

    _, started_error = await _dispatch(
        _TOPIC_SESSION_STARTED, _LIVE_SESSION_STARTED_PAYLOAD
    )
    assert started_error is None, (
        f"session.started dispatch raised {type(started_error).__name__}: "
        f"{started_error}"
    )
    _, ended_error = await _dispatch(_TOPIC_SESSION_ENDED, _SESSION_ENDED_PAYLOAD)
    assert ended_error is None, (
        f"session.ended dispatch raised {type(ended_error).__name__}: {ended_error}"
    )

    state = yaml.safe_load(
        (tmp_path / ".onex_state" / "session" / "phase_state.yaml").read_text()
    )
    assert state["current_phase"] == "ended", (
        "session.ended did not fold onto the state the reducer had already written"
    )
    assert state["session_id"] == "omn16162-live-proof-1787064555"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_arg_is_the_declared_input_model_not_the_raw_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime must hand the handler its contract-declared input model.

    This is the arity/annotation rule the old signature violated. Asserting it at
    the resolver keeps the regression legible: if someone re-adds a second
    positional parameter, or widens the annotation back to ``dict``, the resolver
    silently returns ``None`` again and the handler starts receiving the raw
    materialized envelope -- exactly the OMN-16790 outage.
    """
    monkeypatch.chdir(tmp_path)

    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _resolve_def_b_input_model_type,
    )

    from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
        ModelSessionPhaseReducerInput,
    )

    resolved = _resolve_def_b_input_model_type(HandlerSessionPhaseReducer().handle)

    assert resolved is ModelSessionPhaseReducerInput, (
        "handle() is not a canonical def-B handler; the runtime will pass the raw "
        f"materialized wire dict instead of a validated model (resolved={resolved})"
    )
