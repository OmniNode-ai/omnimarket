# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24177 AC1: one run per model per window, across a restart.

The golden chain runs the window trigger into the real trial driver on the in-memory bus:
two ticks of one window start one run; a restart of the trigger after the send but before the
driver recorded it still starts one run; a restart of the driver after it recorded the run
still starts one. Positive control: the next window's request does start a second run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalRunRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.handlers.handler_code_eval_trial_orchestrator import (
    HandlerCodeEvalTrialOrchestrator,
)
from omnimarket.nodes.node_eval_tracking_schedule_compute.handlers import (
    HandlerEvalTrackingSchedule,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    Bus,
    make_bus,
)
from tests.nodes.node_eval_tracking_schedule_compute.support import (
    DAY,
    EPOCH,
    LOCAL,
    make_overlay,
    tick_at,
    turns,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_eval_tracking_schedule_compute/contract.yaml"
)


def request_at(
    handler: HandlerEvalTrackingSchedule, which: int
) -> ModelCodeEvalRunRequest:
    """The request of the ``which``-th local turn on or after the epoch."""
    at = list(turns(LOCAL, EPOCH, EPOCH + 2 * DAY))[which]
    request = handler.handle(tick_at(at))
    assert request is not None
    return request


def started_runs(bus: Bus) -> set[str]:
    return {unit.lane for unit in bus.units}


def restarted_driver(bus: Bus) -> None:
    """A driver restart: a new handler over the rows the old one recorded."""
    old = bus.handler
    bus.handler = HandlerCodeEvalTrialOrchestrator(
        config=old._config, ports=old._ports, store=old._local_store, clock=old._clock
    )


def test_two_ticks_in_one_window_start_one_run() -> None:
    trigger = HandlerEvalTrackingSchedule(make_overlay())
    bus = make_bus()
    first, second = request_at(trigger, 0), request_at(trigger, 1)
    assert first.correlation_id == second.correlation_id
    assert bus.send(first), "the first request starts the run"
    started = list(bus.units)
    assert bus.send(second) == [], "the second request of the window changes nothing"
    assert len(bus.units) == len(started)
    bus.drain()
    assert started_runs(bus) == {str(first.correlation_id)}
    assert len(bus.closes) == 1


def test_a_restart_of_the_trigger_after_the_send_but_before_the_record_starts_one_run() -> (
    None
):
    bus = make_bus()
    before = HandlerEvalTrackingSchedule(make_overlay())
    lost = request_at(before, 0)  # sent, never reached the driver: nothing recorded
    after = HandlerEvalTrackingSchedule(
        make_overlay()
    )  # the restarted trigger holds no state
    resent = request_at(after, 1)
    assert resent == lost, (
        "the restarted trigger names the same run with the same request"
    )
    assert bus.units == []
    assert bus.send(resent), "the driver never held the run, so the resend starts it"
    bus.drain()
    assert started_runs(bus) == {str(lost.correlation_id)}
    assert len(bus.closes) == 1


def test_a_restart_of_the_driver_after_it_recorded_the_run_starts_one_run() -> None:
    trigger = HandlerEvalTrackingSchedule(make_overlay())
    bus = make_bus()
    assert bus.send(request_at(trigger, 0))
    units_before = len(bus.units)
    restarted_driver(bus)
    assert bus.send(request_at(trigger, 1)) == []
    bus.drain()
    assert len(bus.units) >= units_before
    assert len({u.lane for u in bus.units}) == 1
    assert len(bus.closes) == 1


def test_the_next_window_starts_a_second_run() -> None:
    trigger = HandlerEvalTrackingSchedule(make_overlay())
    bus = make_bus()
    window_0 = request_at(trigger, 0)
    at = next(turns(LOCAL, EPOCH + 7 * DAY, EPOCH + 8 * DAY))
    window_1 = trigger.handle(tick_at(at))
    assert window_1 is not None
    assert bus.send(window_0)
    assert bus.send(window_1)
    bus.drain()
    assert started_runs(bus) == {
        str(window_0.correlation_id),
        str(window_1.correlation_id),
    }
    assert len(bus.closes) == 2


def test_the_run_request_is_published_on_the_contract_topic() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    topic = "onex.cmd.omnimarket.code-eval-run-requested.v1"
    assert contract["event_bus"]["publish_topics"] == [topic]
    assert contract["terminal_event"] == topic
