# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_job_check_schedule_compute: the runtime tick requests one sweep per interval."""

from __future__ import annotations

import datetime as dt
import itertools
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.enums.enum_lab_job_check import EnumLabJobCheckScope
from omnimarket.models.lab_job.model_lab_job_check import ModelLabJobCheckRequested
from omnimarket.nodes.node_lab_job_check_schedule_compute.handlers.handler_lab_job_check_schedule import (
    HandlerLabJobCheckSchedule,
    schedule_config,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[4] / "src" / "omnimarket" / "nodes"
SCHEDULE = NODES / "node_lab_job_check_schedule_compute" / "contract.yaml"
EFFECT = NODES / "node_lab_job_check_effect" / "contract.yaml"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
DAY = dt.datetime(2026, 10, 11, tzinfo=dt.UTC)


def _tick(now: dt.datetime, interval_ms: int = 1000) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=interval_ms,
    )


def test_the_default_interval_is_300_seconds() -> None:
    assert schedule_config().interval_seconds == 300


@pytest.mark.parametrize(
    ("offset_s", "fires"),
    [
        (0, True),  # the tick that opens the window
        (0.5, True),  # still inside tick_interval_ms
        (1, False),  # the next tick of the same window
        (150, False),
        (299, False),
        (300, True),  # the next window
        (301, False),
    ],
)
def test_only_the_tick_that_opens_a_window_requests_a_sweep(
    offset_s: float, fires: bool
) -> None:
    now = DAY + dt.timedelta(hours=7, seconds=offset_s)
    result = HandlerLabJobCheckSchedule().handle(_tick(now))
    assert (result is not None) is fires


def test_the_request_is_a_nonterminal_sweep_named_by_its_window() -> None:
    now = DAY + dt.timedelta(hours=7, minutes=5)
    result = HandlerLabJobCheckSchedule().handle(_tick(now))
    assert isinstance(result, ModelLabJobCheckRequested)
    assert result.scope is EnumLabJobCheckScope.NONTERMINAL
    assert result.requested_at == now
    assert result.check_id == "lab-job-check-20261011T070500Z"


def test_a_replayed_tick_requests_the_same_sweep() -> None:
    now = DAY + dt.timedelta(hours=7)
    handler = HandlerLabJobCheckSchedule()
    first = handler.handle(_tick(now))
    again = handler.handle(_tick(now))
    assert first is not None
    assert again is not None
    assert first.check_id == again.check_id


def test_exactly_one_sweep_per_interval_over_a_day_and_the_handler_is_stateless() -> (
    None
):
    handler = HandlerLabJobCheckSchedule()
    fired = [
        t
        for t in (DAY + dt.timedelta(seconds=s) for s in range(0, 86400, 30))
        if handler.handle(_tick(t, interval_ms=30000)) is not None
    ]
    assert len(fired) == 86400 // 300
    gaps = {(b - a).total_seconds() for a, b in itertools.pairwise(fired)}
    assert gaps == {300.0}


def test_a_tick_with_no_interval_requests_nothing() -> None:
    handler = HandlerLabJobCheckSchedule()
    assert handler.handle(_tick(DAY, interval_ms=100)) is not None
    bad = _tick(DAY).model_copy(update={"tick_interval_ms": 0})
    assert handler.handle(bad) is None


def test_the_schedule_publishes_the_effect_command_topic_and_subscribes_to_the_tick() -> (
    None
):
    schedule = yaml.safe_load(SCHEDULE.read_text())
    effect = yaml.safe_load(EFFECT.read_text())
    command_topic = effect["runtime_dispatch"]["command_topic"]
    assert schedule["event_bus"]["subscribe_topics"] == [TICK_TOPIC]
    assert schedule["event_bus"]["publish_topics"] == [command_topic]
    assert schedule["terminal_event"] == command_topic
    assert command_topic in effect["event_bus"]["subscribe_topics"]
