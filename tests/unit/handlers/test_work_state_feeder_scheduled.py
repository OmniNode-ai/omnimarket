# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled work-state feeder (OMN-23427): the interval gate, the stale bound, the event, the refusals."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.handlers.work_state_feeder_scheduled import (
    HandlerWorkStateFeederScheduled,
)
from omnimarket.models.work_state_feeder import (
    EnumFeeder,
    EnumFeederProfile,
    ModelFeederOutcome,
    ModelFeederScheduleOverlay,
)
from omnimarket.protocols.protocol_work_state_feeder import ProtocolWorkStateFeeder

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
OVERLAY_ID = "node_example_feeder_scheduled_effect"
OVERLAY_FILE_ENV = "ONEX_EXAMPLE_FEEDER_SCHEDULE_OVERLAY"


class FakeFeeder:
    """A port that counts its calls and returns the outcome it was built with."""

    def __init__(self, outcome: ModelFeederOutcome) -> None:
        self.calls = 0
        self.seen: list[datetime] = []
        self._outcome = outcome

    def feed(self, now: datetime) -> ModelFeederOutcome:
        self.calls += 1
        self.seen.append(now)
        return self._outcome


class RefusingFeeder:
    """A port whose feed raises, the way a refused source does."""

    def __init__(self) -> None:
        self.calls = 0

    def feed(self, now: datetime) -> ModelFeederOutcome:
        self.calls += 1
        raise RuntimeError("source tree refused")


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def schedule(**override: object) -> ModelFeederScheduleOverlay:
    base: dict[str, object] = {
        "run_interval_seconds": 60,
        "max_source_age_s": 120,
        "profile": EnumFeederProfile.LIVE.value,
    }
    return ModelFeederScheduleOverlay.model_validate(base | override)


def fed_outcome(**override: object) -> ModelFeederOutcome:
    base: dict[str, object] = {"status": "fed", "source_as_of": T0, "item_count": 3}
    return ModelFeederOutcome.model_validate(base | override)


def handler(
    port: ProtocolWorkStateFeeder | None = None,
    **override: object,
) -> HandlerWorkStateFeederScheduled:
    kwargs: dict[str, object] = {
        "feeder": EnumFeeder.LINEAR,
        "overlay_id": OVERLAY_ID,
        "overlay_file_env": OVERLAY_FILE_ENV,
        "live": lambda _environ: (
            port
            if port is not None
            else (_ for _ in ()).throw(
                AssertionError("the live feeder must not be built")
            )
        ),
        "schedule": schedule(),
        "feeder_port": port,
    }
    return HandlerWorkStateFeederScheduled(**(kwargs | override))  # type: ignore[arg-type]


def refuse_live(_environ: Mapping[str, str] | None) -> ProtocolWorkStateFeeder:
    raise AssertionError("the in-memory profile must not build the live feeder")


def test_golden_a_due_tick_feeds_and_emits_the_event_for_that_tick() -> None:
    port = FakeFeeder(fed_outcome())
    request = tick(T0)
    event = handler(port).handle(request)
    assert event is not None
    assert event.feeder is EnumFeeder.LINEAR
    assert event.computed_at == T0
    assert event.tick_id == str(request.tick_id)
    assert event.profile is EnumFeederProfile.LIVE
    assert event.status == "fed"
    assert event.source_as_of == T0
    assert event.item_count == 3
    assert event.cause == ""


def test_golden_the_interval_gate_feeds_once_per_interval() -> None:
    port = FakeFeeder(fed_outcome())
    h = handler(port)
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=59))) is None
    assert port.calls == 1
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None
    assert port.calls == 2


def test_golden_a_skipped_tick_does_not_move_the_last_run() -> None:
    port = FakeFeeder(fed_outcome())
    h = handler(port)
    h.handle(tick(T0))
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None
    assert port.calls == 2


def test_golden_a_fed_source_older_than_the_max_age_is_stale() -> None:
    """The failing control: the stale bound is what separates fed from stale."""
    source_as_of = T0 - timedelta(seconds=121)
    port = FakeFeeder(fed_outcome(source_as_of=source_as_of))
    event = handler(port).handle(tick(T0))
    assert event is not None
    assert event.status == "stale"
    assert event.source_as_of == source_as_of


def test_golden_a_fed_source_at_the_max_age_is_fed() -> None:
    """Exactly the stale bound old is not over it: the bound is strict, so flipping it moves this test."""
    source_as_of = T0 - timedelta(seconds=120)
    port = FakeFeeder(fed_outcome(source_as_of=source_as_of))
    event = handler(port).handle(tick(T0))
    assert event is not None
    assert event.status == "fed"


def test_golden_raising_the_stale_bound_turns_the_same_reading_fed() -> None:
    """The same newest fact is stale under 120s and fed under 200s: only the bound decides."""
    source_as_of = T0 - timedelta(seconds=121)
    port = FakeFeeder(fed_outcome(source_as_of=source_as_of))
    h = handler(port, schedule=schedule(max_source_age_s=200))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.status == "fed"


def test_error_a_feeder_that_raises_leaves_the_last_run_so_the_next_tick_retries() -> (
    None
):
    port = RefusingFeeder()
    h = handler(port)
    with pytest.raises(RuntimeError, match="source tree refused"):
        h.handle(tick(T0))
    with pytest.raises(RuntimeError, match="source tree refused"):
        h.handle(tick(T0 + timedelta(seconds=1)))
    assert port.calls == 2


def test_golden_the_in_memory_default_profile_reports_not_fed() -> None:
    h = handler(
        schedule=schedule(profile=EnumFeederProfile.IN_MEMORY), live=refuse_live
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.profile is EnumFeederProfile.IN_MEMORY
    assert event.status == "not_fed"
    assert event.source_as_of is None
    assert event.item_count == 0
    assert "in-memory" in event.cause


def test_golden_the_live_factory_receives_the_environ() -> None:
    seen: list[Mapping[str, str] | None] = []

    def live(environ: Mapping[str, str] | None) -> ProtocolWorkStateFeeder:
        seen.append(environ)
        return FakeFeeder(fed_outcome())

    environ = {"OMNI_HOME": "/somewhere"}
    h = handler(live=live, environ=environ, feeder_port=None)
    assert h.handle(tick(T0)) is not None
    assert seen == [environ]


def test_error_an_outcome_fed_without_a_source_as_of_is_refused() -> None:
    with pytest.raises(ValidationError, match="source_as_of"):
        ModelFeederOutcome(status="fed", source_as_of=None, item_count=1)


def test_error_an_outcome_not_fed_without_a_cause_is_refused() -> None:
    with pytest.raises(ValidationError, match="cause"):
        ModelFeederOutcome(status="not_fed", item_count=0)


def test_error_a_zero_interval_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        schedule(run_interval_seconds=0)


def test_error_a_non_positive_stale_bound_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        schedule(max_source_age_s=0)


def test_error_an_unknown_feeder_profile_is_refused() -> None:
    with pytest.raises(ValidationError, match="profile"):
        schedule(profile="database")
