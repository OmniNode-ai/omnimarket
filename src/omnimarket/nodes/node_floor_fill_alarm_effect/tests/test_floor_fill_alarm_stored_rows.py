# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm check reads what a real fill tick stores, and a shape that does not match is refused, never read as zero.

The stored rows here come from a real live-profile tick (``live_tick``) or are built from the fill's own models
(``ModelFloorFillStoredAnswer``, ``ModelFloorFillState``), never written as JSON by hand.

Failing controls: a state row with no ``entries`` is refused (it never reads as no lanes started); a stored answer of the
old shape, with no ``lanes_started``, is refused (it never reads as 0 sent); a gap in the stored answer ``seq`` is refused
and the runs before it are not joined to the runs after it (two idle runs cannot step over a run that sent lanes).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    StoredAnswerRunSource,
    StoredStateSentLanes,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.handler_floor_fill_alarm_check import (
    CELL_SEPARATOR,
    HandlerFloorFillAlarmCheck,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    ModelFloorFillAlarmCheckRequest,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillStatus,
    ModelFloorFillStoredAnswer,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    SCOPE,
    live_tick,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
KEY = "floor:alpha:a1"
LANE = "floor-alpha-a1-0001-a1"
ANSWER_KEY = SCOPE + ":answer"


def _stored(
    seq: int,
    *,
    lanes: int = 0,
    free: int | None = 25,
    minutes_ago: int = 0,
    lines: tuple[str, ...] = ("NOTE line-held:alpha | no-free-slots",),
) -> str:
    return ModelFloorFillStoredAnswer(
        lines=lines,
        status=EnumFloorFillStatus.OK,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        seq=seq,
        state_seq=seq,
        free_slots=free,
        lanes_started=lanes,
    ).model_dump_json()


class _Ledger:
    def __init__(self) -> None:
        self.held: list[str] = []

    def rows(self) -> tuple[str, ...]:
        return tuple(self.held)

    def append(self, row: str) -> None:
        self.held.append(row)


class _Lanes:
    def read_sent_lanes(self) -> tuple[()]:
        return ()


class _Pool:
    def read_pool(self) -> ModelFloorFillPoolRead:
        return ModelFloorFillPoolRead(free_slots=25)


# What a real tick stores.


def test_floor_fill_alarm_reads_the_rows_a_real_tick_stores(tmp_path: Path) -> None:
    _out, _code, store = live_tick(tmp_path, free=2, age_seconds=5)

    def load(key: str) -> str | None:
        return store.load(key).payload

    at = datetime.now(UTC) + timedelta(minutes=1)
    runs = StoredAnswerRunSource(load, SCOPE, tmp_path / "runs.jsonl").read_runs(at)
    assert len(runs) == 1
    assert runs[0].lanes_sent == 2
    assert runs[0].free_slots == 2
    # No UNKNOWN line: the cause falls back to the answer's first line.
    assert runs[0].cause.startswith("NOTE line-filled:")
    lanes = StoredStateSentLanes(load, SCOPE, lambda: ()).read_sent_lanes()
    assert len(lanes) == 2
    assert all(lane.key.startswith("floor:") for lane in lanes)
    assert all(lane.claim_at is None for lane in lanes)


def test_floor_fill_alarm_a_real_tick_that_read_no_pool_stores_no_free_slots(
    tmp_path: Path,
) -> None:
    _out, _code, store = live_tick(tmp_path, free=2, age_seconds=600)
    runs = StoredAnswerRunSource(
        lambda key: store.load(key).payload, SCOPE, tmp_path / "runs.jsonl"
    ).read_runs(datetime.now(UTC) + timedelta(minutes=1))
    assert len(runs) == 1
    assert runs[0].free_slots is None
    assert runs[0].lanes_sent == 0
    assert runs[0].cause.startswith("UNKNOWN pool-unread:pool-snapshot")


def test_floor_fill_alarm_a_real_tick_that_saw_zero_slots_stores_zero(
    tmp_path: Path,
) -> None:
    _out, _code, store = live_tick(tmp_path, free=0, age_seconds=5)
    runs = StoredAnswerRunSource(
        lambda key: store.load(key).payload, SCOPE, tmp_path / "runs.jsonl"
    ).read_runs(datetime.now(UTC) + timedelta(minutes=1))
    assert runs[0].free_slots == 0
    assert runs[0].lanes_sent == 0


# A shape that does not match is refused.


def test_floor_fill_alarm_state_without_entries_is_refused() -> None:
    reader = StoredStateSentLanes({SCOPE: '{"seq": 3}'}.get, SCOPE, lambda: ())
    with pytest.raises(AlarmSourceError, match="entries"):
        reader.read_sent_lanes()


def test_floor_fill_alarm_state_with_entries_of_the_wrong_shape_is_refused() -> None:
    reader = StoredStateSentLanes(
        {SCOPE: '{"seq": 3, "entries": [{"key": "floor:a:b"}]}'}.get,
        SCOPE,
        lambda: (),
    )
    with pytest.raises(AlarmSourceError):
        reader.read_sent_lanes()


def test_floor_fill_alarm_an_empty_state_row_is_no_lanes_not_an_error() -> None:
    reader = StoredStateSentLanes(
        {SCOPE: '{"seq": 0, "entries": {}}'}.get, SCOPE, lambda: ()
    )
    assert reader.read_sent_lanes() == ()


def test_floor_fill_alarm_answer_of_the_old_shape_is_refused(tmp_path: Path) -> None:
    old = (
        '{"lines": ["NOTE line-filled:alpha | lane=la,lb"], "status": "OK", '
        '"observed_at": "2026-11-16T17:00:00+00:00", "seq": 1, "state_seq": 1}'
    )
    source = StoredAnswerRunSource(
        {ANSWER_KEY: old}.get, SCOPE, tmp_path / "runs.jsonl"
    )
    with pytest.raises(AlarmSourceError, match="lanes_started"):
        source.read_runs(NOW)


# Every run is kept, or the gap is refused.


def test_floor_fill_alarm_a_gap_in_the_runs_drops_only_the_idle_history(
    tmp_path: Path,
) -> None:
    store = {ANSWER_KEY: _stored(1, minutes_ago=15)}
    log = tmp_path / "runs.jsonl"
    source = StoredAnswerRunSource(store.get, SCOPE, log)
    assert len(source.read_runs(NOW)) == 1
    # Run 2 sent two lanes and the check did not run; run 3 sent none.
    store[ANSWER_KEY] = _stored(3, minutes_ago=5)
    after = source.read_runs(NOW)
    assert [run.observed_at for run in after] == [NOW - timedelta(minutes=5)], (
        "the gap is read on, with only the runs after it"
    )
    ledger = _Ledger()
    sent = ModelFloorFillSentLane(
        lane=LANE, key=KEY, sent_at=NOW - timedelta(minutes=30)
    )
    handler = HandlerFloorFillAlarmCheck(
        runs=source, lanes=_FixedLanes((sent,)), pool=_Pool(), ledger=ledger
    )
    result = handler.handle(_request())
    assert result.runs_read == 1
    assert result.lanes_read == 1, "a gap does not stop the lanes from being read"
    assert result.pool_free_slots == 25, "a gap does not stop the pool from being read"
    assert result.alarms_found == 1, (
        "run 1 and run 3 are not two idle runs, and the unclaimed lane alarms"
    )
    assert "alarm=floor-fill-unclaimed" in ledger.held[0]
    # Run 4 sends none either: runs 3 and 4 follow each other and the idle alarm is right to fire.
    store[ANSWER_KEY] = _stored(4, minutes_ago=2)
    again = handler.handle(_request())
    assert again.alarms_found == 2


def test_floor_fill_alarm_the_run_log_is_written_whole_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = {ANSWER_KEY: _stored(1, minutes_ago=15)}
    log = tmp_path / "runs.jsonl"
    source = StoredAnswerRunSource(store.get, SCOPE, log)
    source.read_runs(NOW)
    before = log.read_text(encoding="utf-8")
    store[ANSWER_KEY] = _stored(2, lanes=2, minutes_ago=10)
    real = Path.write_text

    def cut_short(self: Path, text: str, *args: object, **kwargs: object) -> int:
        real(self, text[: len(text) // 3], *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", cut_short)
    with pytest.raises(AlarmSourceError, match="not written"):
        source.read_runs(NOW)
    monkeypatch.undo()
    assert log.read_text(encoding="utf-8") == before, (
        "a write cut short left half a log"
    )
    assert [run.lanes_sent for run in source.read_runs(NOW)] == [0, 2]


def test_floor_fill_alarm_a_run_log_cut_mid_write_does_not_fail_every_check(
    tmp_path: Path,
) -> None:
    store = {ANSWER_KEY: _stored(1, minutes_ago=15)}
    log = tmp_path / "runs.jsonl"
    source = StoredAnswerRunSource(store.get, SCOPE, log)
    source.read_runs(NOW)
    log.write_text(log.read_text(encoding="utf-8")[:-40], encoding="utf-8")
    store[ANSWER_KEY] = _stored(2, lanes=1, minutes_ago=10)
    runs = source.read_runs(NOW)
    assert [run.lanes_sent for run in runs] == [1], (
        "the log starts again from the answer"
    )
    assert [run.lanes_sent for run in source.read_runs(NOW)] == [1]
    store[ANSWER_KEY] = _stored(3, minutes_ago=5)
    assert [run.lanes_sent for run in source.read_runs(NOW)] == [1, 0]


def test_floor_fill_alarm_an_old_shape_answer_is_an_alarm_row_not_a_failed_check(
    tmp_path: Path,
) -> None:
    old = (
        '{"lines": ["NOTE line-filled:alpha | lane=la,lb"], "status": "OK", '
        '"observed_at": "2026-11-16T17:00:00+00:00", "seq": 1, "state_seq": 1}'
    )
    source = StoredAnswerRunSource(
        {ANSWER_KEY: old}.get, SCOPE, tmp_path / "runs.jsonl"
    )
    ledger = _Ledger()
    sent = ModelFloorFillSentLane(
        lane=LANE, key=KEY, sent_at=NOW - timedelta(minutes=30)
    )
    result = HandlerFloorFillAlarmCheck(
        runs=source, lanes=_FixedLanes((sent,)), pool=_Pool(), ledger=ledger
    ).handle(_request())
    assert result.sources_unread == ("runs",)
    assert result.lanes_read == 1
    assert len(ledger.held) == 2
    assert any("alarm=floor-fill-source-unread" in row for row in ledger.held)
    assert any("alarm=floor-fill-unclaimed" in row for row in ledger.held)
    # The same unread source on the next check is a repeat, not a new row.
    again = HandlerFloorFillAlarmCheck(
        runs=source, lanes=_FixedLanes((sent,)), pool=_Pool(), ledger=ledger
    ).handle(_request())
    assert again.rows_written == ()


def test_floor_fill_alarm_an_idle_answer_with_no_unknown_line_names_its_first_line_as_the_cause(
    tmp_path: Path,
) -> None:
    answer = _stored(
        1, lines=("NOTE line-held:alpha | no-free-slots", "NOTE line-held:bravo | x")
    )
    (run,) = StoredAnswerRunSource(
        {ANSWER_KEY: answer}.get, SCOPE, tmp_path / "a.jsonl"
    ).read_runs(NOW)
    assert run.cause == "NOTE line-held:alpha | no-free-slots"
    with_unknown = _stored(1, lines=("NOTE a | b", "UNKNOWN pool-unread:unbound:pool"))
    (run,) = StoredAnswerRunSource(
        {ANSWER_KEY: with_unknown}.get, SCOPE, tmp_path / "b.jsonl"
    ).read_runs(NOW)
    assert run.cause == "UNKNOWN pool-unread:unbound:pool"


def test_floor_fill_alarm_runs_in_a_row_are_kept_with_their_slots(
    tmp_path: Path,
) -> None:
    store = {ANSWER_KEY: _stored(1, free=7, minutes_ago=10)}
    source = StoredAnswerRunSource(store.get, SCOPE, tmp_path / "runs.jsonl")
    source.read_runs(NOW)
    store[ANSWER_KEY] = _stored(2, lanes=2, free=None, minutes_ago=5)
    runs = source.read_runs(NOW)
    assert [run.free_slots for run in runs] == [7, None]
    assert [run.lanes_sent for run in runs] == [0, 2]
    assert isinstance(runs[0], ModelFloorFillRun)
    # The same stored answer read again is not a new run.
    assert len(source.read_runs(NOW)) == 2


def test_floor_fill_alarm_a_log_older_than_the_stored_answer_by_one_is_in_a_row(
    tmp_path: Path,
) -> None:
    store = {ANSWER_KEY: _stored(5, minutes_ago=10)}
    source = StoredAnswerRunSource(store.get, SCOPE, tmp_path / "runs.jsonl")
    assert len(source.read_runs(NOW)) == 1
    store[ANSWER_KEY] = _stored(6, minutes_ago=5)
    assert len(source.read_runs(NOW)) == 2


# A CLAIM stamped in the same second as the start.


def _request() -> ModelFloorFillAlarmCheckRequest:
    return ModelFloorFillAlarmCheckRequest(
        now=NOW,
        row_lane="floor-fill-alarm",
        row_ticket="OMN-24873",
        row_principal="floor-fill-alarm-host",
    )


def _state_started_at(started: datetime) -> str:
    return (
        '{"seq": 1, "entries": {"'
        + KEY
        + '": {"key": "'
        + KEY
        + '", "state": "RESERVED", '
        '"lane": "' + LANE + '", "attempt": 1, "ticket": "T-1", "ended": false, '
        '"reserved_at": "' + started.isoformat() + '", "sent_at": null}}}'
    )


def _claim_row(stamp: datetime) -> str:
    return CELL_SEPARATOR.join(
        (
            stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "CLAIM",
            f"lane={LANE}",
            f"key={KEY}",
            "ticket=OMN-1",
        )
    )


def test_floor_fill_alarm_a_claim_in_the_same_second_as_the_start_is_a_claim() -> None:
    started = NOW - timedelta(minutes=20) + timedelta(microseconds=700000)
    rows = (_claim_row(started.replace(microsecond=0)),)
    (lane,) = StoredStateSentLanes(
        {SCOPE: _state_started_at(started)}.get, SCOPE, lambda: rows
    ).read_sent_lanes()
    assert lane.claim_at is not None
    assert lane.claim_at >= lane.sent_at
    handler = HandlerFloorFillAlarmCheck(
        runs=_NoRuns(),
        lanes=_FixedLanes((lane,)),
        pool=_Pool(),
        ledger=_Ledger(),
    )
    result = handler.handle(
        ModelFloorFillAlarmCheckRequest(
            now=NOW,
            row_lane="floor-fill-alarm",
            row_ticket="OMN-24873",
            row_principal="floor-fill-alarm-host",
        )
    )
    assert result.alarms_found == 0, "a same-second CLAIM is on time, not 'no CLAIM'"


def test_floor_fill_alarm_a_claim_in_the_second_before_the_start_is_no_claim() -> None:
    started = NOW - timedelta(minutes=20) + timedelta(microseconds=700000)
    rows = (_claim_row(started.replace(microsecond=0) - timedelta(seconds=1)),)
    (lane,) = StoredStateSentLanes(
        {SCOPE: _state_started_at(started)}.get, SCOPE, lambda: rows
    ).read_sent_lanes()
    assert lane.claim_at is None


class _NoRuns:
    def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
        return ()


class _FixedLanes:
    def __init__(self, lanes: tuple[ModelFloorFillSentLane, ...]) -> None:
        self._lanes = lanes

    def read_sent_lanes(self) -> tuple[ModelFloorFillSentLane, ...]:
        return self._lanes


# A late CLAIM alarms once.


def test_floor_fill_alarm_a_late_claim_alarms_once_however_long_the_window(
    tmp_path: Path,
) -> None:
    del tmp_path
    started = NOW - timedelta(minutes=30)
    unclaimed = ModelFloorFillSentLane(lane=LANE, key=KEY, sent_at=started)
    late = ModelFloorFillSentLane(
        lane=LANE, key=KEY, sent_at=started, claim_at=started + timedelta(minutes=15)
    )
    ledger = _Ledger()

    def check(at: datetime, lane: ModelFloorFillSentLane) -> int:
        result = HandlerFloorFillAlarmCheck(
            runs=_NoRuns(), lanes=_FixedLanes((lane,)), pool=_Pool(), ledger=ledger
        ).handle(
            ModelFloorFillAlarmCheckRequest(
                now=at,
                row_lane="floor-fill-alarm",
                row_ticket="OMN-24873",
                row_principal="floor-fill-alarm-host",
            )
        )
        return len(result.rows_written)

    assert check(NOW, unclaimed) == 1
    # The CLAIM then arrives late: a different cause than "no CLAIM", written once.
    assert check(NOW + timedelta(minutes=5), late) == 1
    assert check(NOW + timedelta(minutes=70), late) == 0
    assert check(NOW + timedelta(hours=5), late) == 0
    assert len(ledger.held) == 2
    assert any("arrived" in row for row in ledger.held)
    assert re.search(r"alarm=floor-fill-unclaimed", ledger.held[-1])
