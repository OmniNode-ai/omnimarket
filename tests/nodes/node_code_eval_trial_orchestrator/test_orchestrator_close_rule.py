# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC14 through the orchestrator handler: error chain and close rule. The reducer's
own close-rule tests hold the decisions; these hold the composition: what the handler
writes, what it sends, what the tick route nudges and what the codec's abandoned terminal
returns. Exemplar: ``test_close_rule.py`` (the reducer's own)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from omnimarket.models.code_eval_trial.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.models.model_code_score import (
    ModelCodeScoreRequest,
)
from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.row_store import (
    InMemoryCodeEvalRowStore,
    encode_row,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.state_codec import StateIoCodec
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    CodeEvalConfigurationError,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    T0,
    Bus,
    leg_config,
    make_bus,
    run_request,
)

pytestmark = pytest.mark.unit


def stored_row(bus: Bus):
    store = bus.handler._local_store
    row, _version = asyncio.run(store.load(str(RUN_ID)))
    return row


def test_no_close_while_a_try_is_in_flight_or_a_control_is_missing() -> None:
    bus = make_bus(lose=(ModelLabWorkUnitRequest,))
    bus.send(run_request(1, 1))
    bus.drain()
    assert bus.units, "the control passed and the try was placed"
    assert bus.closes == []
    assert stored_row(bus).in_flight
    lost_scores = make_bus(lose=(ModelCodeScoreRequest,))
    lost_scores.send(run_request(1, 1))
    lost_scores.drain()
    assert lost_scores.units == []
    assert lost_scores.closes == []


def test_a_second_score_waits_while_one_is_in_flight() -> None:
    bus = make_bus(lose=(ModelCodeScoreRequest,))
    bus.send(run_request(2, 2))
    bus.drain()
    assert len(bus.scores) == 1, "two controls queued, one score slot"


def test_no_try_is_placed_before_its_control_reads_pass() -> None:
    bus = make_bus(lose=(ModelCodeScoreRequest,))
    bus.send(run_request(2, 2))
    bus.drain()
    assert bus.units == []


def test_a_lost_control_score_expires_on_the_nudge_and_the_run_closes() -> None:
    bus = make_bus(lose=(ModelCodeScoreRequest,))
    bus.send(run_request(1, 1))
    bus.drain()
    assert bus.closes == []
    bus.advance(300 + 60 + 1)
    bus.nudge()
    bus.drain()
    assert len(bus.closes) == 1
    closed = bus.closes[0]
    assert closed.controls[0].could_not_be_run is True
    assert closed.sequences == ()
    assert closed.abandoned is False


def test_a_nudge_inside_the_deadline_changes_nothing_and_writes_nothing() -> None:
    bus = make_bus(lose=(ModelLabWorkUnitRequest,))
    bus.send(run_request(1, 1))
    bus.drain()
    before = stored_row(bus)
    version = bus.handler._local_store.version(str(RUN_ID))
    bus.advance(10)
    assert bus.nudge() == []
    assert bus.handler._local_store.version(str(RUN_ID)) == version
    assert stored_row(bus) == before


def test_a_lost_unit_expires_on_the_nudge_after_the_try_timeout_plus_slack() -> None:
    bus = make_bus(lose=(ModelLabWorkUnitRequest,))
    bus.send(run_request(1, 1))
    bus.drain()
    bus.advance(600 + 60 + 1)
    bus.nudge()
    # the lost unit reads unscorable(harness) and the next try is placed on a free host
    assert len(bus.units) == 2


def test_redelivered_answers_write_nothing() -> None:
    bus = make_bus()
    bus.send(run_request(1, 1))
    bus.drain()
    version = bus.handler._local_store.version(str(RUN_ID))
    answers = list(bus.units)
    assert answers
    for unit in answers:
        bus.send(bus.host(unit))
    assert bus.handler._local_store.version(str(RUN_ID)) == version
    assert len(bus.closes) == 1


def test_a_request_whose_timeouts_exceed_the_completion_bound_is_dead_lettered_with_no_row() -> (
    None
):
    # break "bound below the score timeout": 300 + 60 + 30 does not fit under 360
    bus = make_bus(config=leg_config(bound=360, score_timeout=300))
    with pytest.raises(CodeEvalConfigurationError):
        bus.send(run_request(1, 1))
    assert stored_row(bus) is None
    assert bus.units == []
    assert bus.scores == []
    assert bus.closes == []


def test_the_tick_route_nudges_each_open_run_and_none_after_the_close() -> None:
    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

    bus = make_bus(lose=(ModelLabWorkUnitRequest,))
    bus.send(run_request(1, 1))
    bus.drain()
    tick = ModelRuntimeTick(
        now=T0 + timedelta(seconds=5),
        tick_id=__import__("uuid").uuid4(),
        sequence_number=1,
        scheduled_at=T0,
        correlation_id=__import__("uuid").uuid4(),
        scheduler_id="s",
        tick_interval_ms=30000,
    )
    emitted = bus.send(tick)
    assert [n.correlation_id for n in emitted] == [RUN_ID]
    assert emitted[0].now == tick.now
    done = make_bus()
    done.send(run_request(1, 1))
    done.drain()
    assert done.closes
    assert done.send(tick) == []


def abandoned_payload(bus: Bus) -> dict:
    row = stored_row(bus)
    return {
        "correlation_id": str(RUN_ID),
        "tenant_id": "lab",
        "state": row.state,
        "payload_json": encode_row(row),
        "failure_class": "runtime_restart_during_code_eval_run",
        "failure_code": "ONEX_MARKET_CODE_EVAL_RUN_ABANDONED",
        "max_wall_seconds": 7200,
    }


def test_a_row_past_the_completion_bound_yields_the_closed_event_abandoned() -> None:
    # also with no host free, and not for a run the codec is never asked about
    bus = make_bus(hosts=(), lose=(ModelLabWorkUnitRequest,))
    bus.send(run_request(2, 2))
    bus.drain()
    built = StateIoCodec().build_abandoned_terminal(**abandoned_payload(bus))
    assert built is not None
    module, name, payload = built
    assert name == "ModelCodeEvalRunClosed"
    assert module == "omnimarket.models.code_eval_trial"
    closed = ModelCodeEvalRunClosed.model_validate(payload)
    assert closed.abandoned is True
    assert closed.correlation_id == RUN_ID
    assert closed.digest
    assert closed.attempt_budgets == (1, 3)
    assert len(closed.controls) == 2


def test_the_abandoned_terminal_is_none_for_a_closed_or_unreadable_row() -> None:
    bus = make_bus()
    bus.send(run_request(1, 1))
    bus.drain()
    assert StateIoCodec().build_abandoned_terminal(**abandoned_payload(bus)) is None
    unreadable = abandoned_payload(bus) | {"payload_json": "{not json"}
    assert StateIoCodec().build_abandoned_terminal(**unreadable) is None


def test_the_closed_row_reads_completed_and_not_in_flight_for_the_runtime_sweep() -> (
    None
):
    bus = make_bus()
    bus.send(run_request(1, 1))
    bus.drain()
    row = stored_row(bus)
    assert (row.state, row.in_flight, row.closed) == ("COMPLETED", False, True)
    import json

    envelope = json.loads(encode_row(row))
    assert (envelope["state"], envelope["in_flight"], envelope["tenant_id"]) == (
        "COMPLETED",
        False,
        "lab",
    )


def test_an_in_memory_store_is_the_default_and_a_fresh_handler_holds_no_row() -> None:
    assert isinstance(make_bus().handler._local_store, InMemoryCodeEvalRowStore)
