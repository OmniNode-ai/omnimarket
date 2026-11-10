# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of node_work_state_linear_feeder_scheduled_effect (OMN-23427).

Golden: a due tick feeds and the event names the linear feeder, the status and the newest fact. Error chains: a feed that failed
is reported with its cause, a feed older than the bound is stale, a feed that raises leaves the last run so the next tick tries
again. The parametrized bus and contract chains of all three feeders are in tests/nodes/node_work_state_feeder_nodes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.work_state_feeder import (
    EnumFeeder,
    EnumFeederProfile,
    ModelFeederOutcome,
    ModelFeederScheduleOverlay,
)
from omnimarket.nodes.node_work_state_linear_feeder_scheduled_effect.handlers.handler_linear_feeder_scheduled import (
    HandlerWorkStateLinearFeederScheduled,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
SCHEDULE = ModelFeederScheduleOverlay(
    run_interval_seconds=60, max_source_age_s=3600, profile=EnumFeederProfile.LIVE
)


def _tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


class _Port:
    def __init__(self, *outcomes: ModelFeederOutcome | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def feed(self, now: datetime) -> ModelFeederOutcome:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_golden_a_due_tick_feeds_and_the_event_names_the_feeder() -> None:
    fresh = ModelFeederOutcome(
        status="fed", source_as_of=T0 - timedelta(minutes=5), item_count=7
    )
    event = HandlerWorkStateLinearFeederScheduled(
        schedule=SCHEDULE, feeder_port=_Port(fresh)
    ).handle(_tick(T0))
    assert event is not None
    assert event.feeder is EnumFeeder.LINEAR
    assert event.status == "fed"
    assert event.item_count == 7
    assert event.source_as_of == T0 - timedelta(minutes=5)


def test_error_a_feed_that_failed_is_reported_with_its_cause() -> None:
    failed = ModelFeederOutcome(
        status="failed", item_count=0, cause="source unreadable"
    )
    event = HandlerWorkStateLinearFeederScheduled(
        schedule=SCHEDULE, feeder_port=_Port(failed)
    ).handle(_tick(T0))
    assert event is not None
    assert event.status == "failed"
    assert event.cause == "source unreadable"


def test_error_a_feed_older_than_the_bound_is_stale_never_fresh() -> None:
    old = ModelFeederOutcome(
        status="fed", source_as_of=T0 - timedelta(hours=2), item_count=3
    )
    event = HandlerWorkStateLinearFeederScheduled(
        schedule=SCHEDULE, feeder_port=_Port(old)
    ).handle(_tick(T0))
    assert event is not None
    assert event.status == "stale"


def test_error_a_feed_that_raises_leaves_the_last_run_so_the_next_tick_retries() -> (
    None
):
    fresh = ModelFeederOutcome(status="fed", source_as_of=T0, item_count=1)
    port = _Port(RuntimeError("down"), fresh)
    handler = HandlerWorkStateLinearFeederScheduled(schedule=SCHEDULE, feeder_port=port)
    with pytest.raises(RuntimeError):
        handler.handle(_tick(T0))
    event = handler.handle(_tick(T0 + timedelta(seconds=1)))
    assert event is not None
    assert port.calls == 2
