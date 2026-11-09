# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The release predicate of the andon stop (OMN-22941, AC1).

A stop clears only with a go-and-see row and a WORKING reading, both dated after the stop and the
reading also after the newest lease. Two fixed readings alone leave it open: that is the failing
control, the shape the reducer cleared on before the predicate existed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.rules_stop_gate import (
    fold_stop_event,
    load_andon_stop_contract,
    release_predicate,
)
from omnimarket.models.stop_gate import (
    EnumFloorVerdict,
    EnumFoldStatus,
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumReleaseReason,
    EnumStopEventKind,
    ModelGoAndSeeRow,
    ModelStopEvent,
    ModelStopReading,
    ModelStopRecord,
    ModelStopScope,
    ModelWorkingReading,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
FSM, CONFIG = load_andon_stop_contract()
FIXED_READING = ModelStopReading(
    verdict=EnumReadingVerdict.PASS,
    proof_eligible=True,
    origin=EnumReadingOrigin.EXECUTED,
    covers_scope=True,
)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def stop(**fields: Any) -> ModelStopRecord:
    return ModelStopRecord(
        id="stop-1",
        cls="engine-policy-breach",
        scope=ModelStopScope(kind="engine", key="example-engine"),
        state=fields.pop("state", "fixed"),
        stopped_at=fields.pop("stopped_at", T0),
        fixed_at=T0,
        **fields,
    )


def working(minutes: int, verdict: EnumFloorVerdict = EnumFloorVerdict.WORKING) -> Any:
    return ModelWorkingReading(verdict=verdict, read_at=at(minutes))


def row(minutes: int, observed: str = "went to the scope and saw it work") -> Any:
    return ModelGoAndSeeRow(
        row_id=f"gas-{minutes}",
        lane_id="lane-1",
        seen_at=at(minutes),
        observed=observed,
    )


def readback(
    minutes: int,
    *,
    go_and_see: ModelGoAndSeeRow | None,
    reading_working: ModelWorkingReading | None,
) -> ModelStopEvent:
    return ModelStopEvent(
        stop_id="stop-1",
        kind=EnumStopEventKind.READBACK,
        role="readback",
        at=at(minutes),
        reading=FIXED_READING.model_copy(update={"working": reading_working}),
        go_and_see=go_and_see,
    )


def test_release_predicate_two_fixed_readings_alone_leave_the_stop_open() -> None:
    record = stop()
    for minutes in (1, 2, 3):
        result = fold_stop_event(
            record,
            readback(minutes, go_and_see=None, reading_working=None),
            FSM,
            CONFIG,
        )
        assert result.status is EnumFoldStatus.APPLIED
        record = result.stop
    assert record.fixed_readings_seen == 3
    assert record.state == "fixed"
    assert record.cleared_at is None
    assert result.release is None
    assert result.release_reason is EnumReleaseReason.GO_AND_SEE_MISSING
    assert "release=go_and_see_missing" in result.line


def test_release_predicate_go_and_see_and_working_reading_clear_the_stop() -> None:
    record = stop(last_lease_at=at(0))
    first = fold_stop_event(
        record,
        readback(1, go_and_see=row(1), reading_working=working(1)),
        FSM,
        CONFIG,
    )
    assert first.stop.state == "fixed"
    assert first.release is None
    second = fold_stop_event(
        first.stop,
        readback(2, go_and_see=row(1), reading_working=working(2)),
        FSM,
        CONFIG,
    )
    assert second.stop.state == "cleared"
    assert second.stop.cleared_at == at(2)
    assert second.release is not None
    assert second.release.go_and_see_row_id == "gas-1"
    assert second.release.go_and_see_lane_id == "lane-1"
    assert second.release.working_read_at == at(2)
    assert second.release.released_at == at(2)
    assert second.release_reason is None


@pytest.mark.parametrize(
    ("go_and_see", "reading_working", "lease", "reason"),
    [
        (None, working(5), None, EnumReleaseReason.GO_AND_SEE_MISSING),
        (row(5, observed="  "), working(5), None, EnumReleaseReason.GO_AND_SEE_EMPTY),
        (row(0), working(5), None, EnumReleaseReason.GO_AND_SEE_BEFORE_STOP),
        (row(5), None, None, EnumReleaseReason.WORKING_READING_MISSING),
        (
            row(5),
            working(5, EnumFloorVerdict.UNKNOWN),
            None,
            EnumReleaseReason.WORKING_READING_NOT_WORKING,
        ),
        (
            row(5),
            working(5, EnumFloorVerdict.BROKEN),
            None,
            EnumReleaseReason.WORKING_READING_NOT_WORKING,
        ),
        (row(5), working(0), None, EnumReleaseReason.WORKING_READING_BEFORE_STOP),
        (row(5), working(3), at(3), EnumReleaseReason.WORKING_READING_BEFORE_LEASE),
        (row(5), working(2), at(3), EnumReleaseReason.WORKING_READING_BEFORE_LEASE),
    ],
)
def test_release_predicate_each_missing_part_holds_the_stop_open(
    go_and_see: ModelGoAndSeeRow | None,
    reading_working: ModelWorkingReading | None,
    lease: datetime | None,
    reason: EnumReleaseReason,
) -> None:
    record = stop(last_lease_at=lease)
    verdict = release_predicate(
        record, readback(6, go_and_see=go_and_see, reading_working=reading_working)
    )
    assert verdict.met is False
    assert verdict.reason is reason


def test_release_predicate_a_reading_dated_after_the_newest_lease_releases() -> None:
    record = stop(last_lease_at=at(3))
    verdict = release_predicate(
        record, readback(6, go_and_see=row(4), reading_working=working(4))
    )
    assert verdict.met is True
    assert verdict.reason is EnumReleaseReason.MET


def test_release_predicate_a_new_lease_after_the_reading_makes_the_reading_stale() -> (
    None
):
    granted = fold_stop_event(
        stop(state="stale"),
        ModelStopEvent(
            stop_id="stop-1",
            kind=EnumStopEventKind.LEASE_GRANTED,
            role="swarm_lane",
            at=at(10),
            lane_id="lane-2",
        ),
        FSM,
        CONFIG,
    )
    assert granted.stop.last_lease_at == at(10)
    verdict = release_predicate(
        granted.stop, readback(11, go_and_see=row(11), reading_working=working(9))
    )
    assert verdict.reason is EnumReleaseReason.WORKING_READING_BEFORE_LEASE
