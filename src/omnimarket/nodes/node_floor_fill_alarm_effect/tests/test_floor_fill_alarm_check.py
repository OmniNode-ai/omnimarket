# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled floor fill alarm check writes the alarm row: IDLE from an independent pool read, an unread pool, a lane
with no CLAIM, each as one STATUS row in the ledger, and a repeat inside the window is not written again.

Failing controls: a quiet floor writes no row; a source that cannot be read fails the check and writes no row.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    CallablePoolReader,
    CommandLedger,
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
    ProtocolAlarmLedger,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
CAUSE = "UNKNOWN pool-unread:pool-snapshot:h1: accounts-unread: token pool command rc=3"
KEY = "floor:alpha:a1"
LANE = "floor-alpha-a1-0001-a1"


class _Runs:
    def __init__(self, runs: tuple[ModelFloorFillRun, ...]) -> None:
        self._runs = runs

    def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
        return self._runs


class _Lanes:
    def __init__(self, lanes: tuple[ModelFloorFillSentLane, ...] = ()) -> None:
        self._lanes = lanes

    def read_sent_lanes(self) -> tuple[ModelFloorFillSentLane, ...]:
        return self._lanes


class _Pool:
    def __init__(self, read: ModelFloorFillPoolRead) -> None:
        self._read = read

    def read_pool(self) -> ModelFloorFillPoolRead:
        return self._read


class _Ledger:
    def __init__(self, rows: tuple[str, ...] = ()) -> None:
        self.held = list(rows)

    def rows(self) -> tuple[str, ...]:
        return tuple(self.held)

    def append(self, row: str) -> None:
        self.held.append(row)


def _runs(
    *, free: int | None = None, cause: str = CAUSE
) -> tuple[ModelFloorFillRun, ...]:
    return tuple(
        ModelFloorFillRun(
            observed_at=NOW - timedelta(minutes=m),
            free_slots=free,
            lanes_sent=0,
            cause=cause,
        )
        for m in (10, 5)
    )


def _request(**extra: object) -> ModelFloorFillAlarmCheckRequest:
    return ModelFloorFillAlarmCheckRequest(
        now=NOW,
        row_lane="floor-fill-alarm",
        row_ticket="OMN-24873",
        **extra,
    )


def _handler(
    runs: tuple[ModelFloorFillRun, ...],
    pool: ModelFloorFillPoolRead,
    ledger: ProtocolAlarmLedger,
    lanes: tuple[ModelFloorFillSentLane, ...] = (),
) -> HandlerFloorFillAlarmCheck:
    return HandlerFloorFillAlarmCheck(
        runs=_Runs(runs), lanes=_Lanes(lanes), pool=_Pool(pool), ledger=ledger
    )


def test_floor_fill_alarm_check_writes_the_idle_row_from_the_pool_read() -> None:
    ledger = _Ledger()
    result = _handler(
        _runs(free=None), ModelFloorFillPoolRead(free_slots=25), ledger
    ).handle(_request())
    assert result.alarms_found == 1
    assert len(ledger.held) == 1
    row = ledger.held[0]
    cells = row.split(CELL_SEPARATOR)
    assert cells[0] == "2026-11-16T17:00:00Z"
    assert cells[1] == "STATUS"
    assert "lane=floor-fill-alarm" in cells
    assert "ticket=OMN-24873" in cells
    assert "alarm=floor-fill-idle" in cells
    assert any(c.startswith("alarm-key=") for c in cells)
    assert CAUSE in cells[-1]
    assert "pool read 25" in cells[-1]
    assert result.rows_written == (row,)


def test_floor_fill_alarm_check_writes_the_unread_pool_row() -> None:
    ledger = _Ledger()
    unread = ModelFloorFillPoolRead(free_slots=None, unread_cause=CAUSE)
    result = _handler(_runs(), unread, ledger).handle(_request())
    assert result.pool_free_slots is None
    assert len(ledger.held) == 1
    assert "alarm=floor-fill-pool-unread" in ledger.held[0]
    assert "accounts-unread" in ledger.held[0]


def test_floor_fill_alarm_check_zero_pool_and_quiet_floor_write_no_row() -> None:
    ledger = _Ledger()
    result = _handler(
        _runs(free=0), ModelFloorFillPoolRead(free_slots=0), ledger
    ).handle(_request())
    assert result.alarms_found == 0
    assert ledger.held == []


def test_floor_fill_alarm_check_writes_the_unclaimed_lane_row() -> None:
    ledger = _Ledger()
    lane = ModelFloorFillSentLane(
        lane=LANE, key=KEY, sent_at=NOW - timedelta(minutes=12)
    )
    sent = tuple(
        ModelFloorFillRun(observed_at=NOW - timedelta(minutes=m), lanes_sent=1)
        for m in (10, 5)
    )
    result = _handler(
        sent, ModelFloorFillPoolRead(free_slots=2), ledger, (lane,)
    ).handle(_request())
    assert result.lanes_read == 1
    assert len(ledger.held) == 1
    assert "alarm=floor-fill-unclaimed" in ledger.held[0]
    assert f"lane {LANE} no CLAIM key={KEY} 12 minutes" in ledger.held[0]


def test_floor_fill_alarm_check_does_not_repeat_a_row_inside_the_window() -> None:
    ledger = _Ledger()
    lane = ModelFloorFillSentLane(
        lane=LANE, key=KEY, sent_at=NOW - timedelta(minutes=12)
    )
    handler = _handler((), ModelFloorFillPoolRead(free_slots=2), ledger, (lane,))
    first = handler.handle(_request())
    again = HandlerFloorFillAlarmCheck(
        runs=_Runs(()),
        lanes=_Lanes((lane,)),
        pool=_Pool(ModelFloorFillPoolRead(free_slots=2)),
        ledger=ledger,
    ).handle(
        ModelFloorFillAlarmCheckRequest(
            now=NOW + timedelta(minutes=5),
            row_lane="floor-fill-alarm",
            row_ticket="OMN-24873",
        )
    )
    assert len(first.rows_written) == 1
    assert again.alarms_found == 1
    assert again.rows_written == ()
    assert again.rows_suppressed == 1
    assert len(ledger.held) == 1
    later = HandlerFloorFillAlarmCheck(
        runs=_Runs(()),
        lanes=_Lanes((lane,)),
        pool=_Pool(ModelFloorFillPoolRead(free_slots=2)),
        ledger=ledger,
    ).handle(
        ModelFloorFillAlarmCheckRequest(
            now=NOW + timedelta(minutes=90),
            row_lane="floor-fill-alarm",
            row_ticket="OMN-24873",
        )
    )
    assert len(later.rows_written) == 1
    assert len(ledger.held) == 2


def test_floor_fill_alarm_check_a_source_that_cannot_be_read_writes_no_row() -> None:
    class _Broken:
        def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
            raise AlarmSourceError("stored answer unreadable")

    ledger = _Ledger()
    handler = HandlerFloorFillAlarmCheck(
        runs=_Broken(),
        lanes=_Lanes(),
        pool=_Pool(ModelFloorFillPoolRead(free_slots=25)),
        ledger=ledger,
    )
    with pytest.raises(AlarmSourceError):
        handler.handle(_request())
    assert ledger.held == []


# Adapters over what the fill leaves behind.

SCOPE = "floor-fill"


def _answer(seq: int, minutes_ago: int, lines: list[str]) -> str:
    import json

    return json.dumps(
        {
            "lines": lines,
            "status": "OK",
            "observed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "seq": seq,
            "state_seq": seq,
        }
    )


def test_floor_fill_alarm_check_run_source_accumulates_each_stored_answer(
    tmp_path: Path,
) -> None:
    store = {SCOPE + ":answer": _answer(1, 10, [CAUSE])}
    source = StoredAnswerRunSource(store.get, SCOPE, tmp_path / "runs.jsonl")
    assert len(source.read_runs(NOW)) == 1
    assert len(source.read_runs(NOW)) == 1
    store[SCOPE + ":answer"] = _answer(2, 5, ["NOTE line-filled:alpha | lane=la,lb"])
    runs = source.read_runs(NOW)
    assert [run.lanes_sent for run in runs] == [0, 2]
    assert runs[0].cause == CAUSE
    assert runs[0].free_slots is None


def test_floor_fill_alarm_check_sent_lanes_carry_the_claim_time(tmp_path: Path) -> None:
    import json

    started = (NOW - timedelta(minutes=20)).isoformat()
    state = json.dumps(
        {
            "seq": 3,
            "entries": {
                KEY: {
                    "key": KEY,
                    "state": "RESERVED",
                    "lane": LANE,
                    "reserved_at": started,
                },
                "floor:alpha:a2": {
                    "key": "floor:alpha:a2",
                    "state": "RESERVED",
                    "lane": "lane-two",
                    "reserved_at": started,
                },
                "stopped:x": {
                    "key": "stopped:x",
                    "state": "RESERVED",
                    "lane": "s",
                    "reserved_at": started,
                },
            },
        }
    )
    claim_ts = (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = (f"{claim_ts} | CLAIM | lane={LANE} | key={KEY} | ticket=OMN-1",)
    lanes = StoredStateSentLanes(
        {SCOPE: state}.get, SCOPE, lambda: rows
    ).read_sent_lanes()
    by_lane = {lane.lane: lane for lane in lanes}
    assert set(by_lane) == {LANE, "lane-two"}
    assert by_lane[LANE].claim_at is not None
    assert by_lane["lane-two"].claim_at is None


def test_floor_fill_alarm_check_pool_reader_distinguishes_unread_from_zero() -> None:
    def zero() -> int:
        return 0

    def broken() -> int:
        raise OSError("snapshot gone")

    assert CallablePoolReader(zero, (OSError,)).read_pool().free_slots == 0
    unread = CallablePoolReader(broken, (OSError,)).read_pool()
    assert unread.free_slots is None
    assert "snapshot gone" in unread.unread_cause


def test_floor_fill_alarm_check_writes_the_row_through_the_append_command(
    tmp_path: Path,
) -> None:
    ledger_file = tmp_path / "ledger.md"
    ledger_file.write_text("", encoding="utf-8")
    appender = tmp_path / "append.py"
    appender.write_text(
        "import sys\nopen(sys.argv[1], 'a', encoding='utf-8').write(sys.argv[2] + '\\n')\n",
        encoding="utf-8",
    )
    ledger = CommandLedger(
        ledger_file, (sys.executable, str(appender), str(ledger_file), "{row}")
    )
    result = _handler(
        _runs(free=None),
        ModelFloorFillPoolRead(free_slots=25),
        ledger,
    ).handle(_request())
    written = ledger_file.read_text(encoding="utf-8").splitlines()
    assert len(written) == 1
    assert written == list(result.rows_written)
    assert "alarm=floor-fill-idle" in written[0]


def test_floor_fill_alarm_check_a_refused_append_fails_the_check(
    tmp_path: Path,
) -> None:
    ledger_file = tmp_path / "ledger.md"
    ledger_file.write_text("", encoding="utf-8")
    ledger = CommandLedger(
        ledger_file, (sys.executable, "-c", "import sys; sys.exit(3)", "{row}")
    )
    with pytest.raises(AlarmSourceError):
        _handler(
            _runs(free=None),
            ModelFloorFillPoolRead(free_slots=25),
            ledger,
        ).handle(_request())
    assert ledger_file.read_text(encoding="utf-8") == ""
