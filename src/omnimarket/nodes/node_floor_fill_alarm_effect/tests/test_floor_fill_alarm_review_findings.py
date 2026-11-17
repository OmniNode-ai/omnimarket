# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm check after a restart, a lane that died, clock skew, a UTC stamp, a bare construction and a scheduled call.

Each test is built from what a real live-profile tick stores (``live_tick``) or from the fill's own models.

Failing controls: with the first CLAIM kept after a restart the new CLAIM is hidden; a lane that ended with no CLAIM is
never alarmed; a run stamped after the check raises where it must be logged; a stamp in another zone is not converted; a
handler built with no ports builds and refuses by name; a tick whose overlay holds no alarm section runs no check.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.container.container_live import (
    run_alarm_check,
    store_payload_loader,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    StoredStateSentLanes,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.handler_floor_fill_alarm_check import (
    CELL_SEPARATOR,
    HandlerFloorFillAlarmCheck,
    render_row,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    ModelFloorFillAlarmCheckRequest,
    load_floor_fill_alarm_settings,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_alarm_section import (
    alarm_section,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_ledger_grammar import (
    AUTHENTICATED_PRINCIPAL,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillEntryResult,
    ModelFloorFillStoredAnswer,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    SCOPE,
    live_tick,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
FAILED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]


def _request(now: datetime = NOW, **extra: object) -> ModelFloorFillAlarmCheckRequest:
    return ModelFloorFillAlarmCheckRequest(
        now=now,
        row_lane="floor-fill-alarm",
        row_ticket="OMN-24873",
        row_principal=AUTHENTICATED_PRINCIPAL,
        **extra,
    )


class _Ledger:
    def __init__(self) -> None:
        self.held: list[str] = []

    def rows(self) -> tuple[str, ...]:
        return tuple(self.held)

    def append(self, row: str) -> None:
        self.held.append(row)


class _Runs:
    def __init__(self, runs: tuple[ModelFloorFillRun, ...] = ()) -> None:
        self._runs = runs

    def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
        return self._runs


class _Lanes:
    def __init__(self, lanes: tuple[ModelFloorFillSentLane, ...] = ()) -> None:
        self._lanes = lanes

    def read_sent_lanes(self) -> tuple[ModelFloorFillSentLane, ...]:
        return self._lanes


class _Pool:
    def read_pool(self) -> ModelFloorFillPoolRead:
        return ModelFloorFillPoolRead(free_slots=25)


def _claim(stamp: datetime, lane: str, key: str) -> str:
    return CELL_SEPARATOR.join(
        (
            stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "CLAIM",
            f"lane={lane}",
            f"key={key}",
            "ticket=OMN-1",
        )
    )


def _real_tick_lane(tmp_path: Path) -> tuple[str, str, datetime]:
    """One lane the real tick started: its lane, its key and the start the state row holds."""
    _out, _code, store = live_tick(tmp_path, free=2, age_seconds=5)
    (lane, *_rest) = StoredStateSentLanes(
        store_payload_loader(store), SCOPE, lambda: ()
    ).read_sent_lanes()
    return lane.lane, lane.key, lane.sent_at


# Finding 4: a restart leaves the old CLAIM first in the ledger.


def test_floor_fill_alarm_the_claim_after_a_restart_is_found_behind_the_old_claim(
    tmp_path: Path,
) -> None:
    _out, _code, store = live_tick(tmp_path, free=2, age_seconds=5)
    load = store_payload_loader(store)
    (first, *_rest) = StoredStateSentLanes(load, SCOPE, lambda: ()).read_sent_lanes()
    old = _claim(first.sent_at - timedelta(minutes=50), first.lane, first.key)
    new = _claim(first.sent_at + timedelta(seconds=90), first.lane, first.key)
    lanes = StoredStateSentLanes(load, SCOPE, lambda: (old, new)).read_sent_lanes()
    found = next(lane for lane in lanes if lane.key == first.key)
    assert found.claim_at is not None, (
        "the old CLAIM hid the new one: a false UNCLAIMED"
    )
    assert found.claim_at >= found.sent_at
    only_old = StoredStateSentLanes(load, SCOPE, lambda: (old,)).read_sent_lanes()
    assert next(lane for lane in only_old if lane.key == first.key).claim_at is None


# Finding 5: a lane that ended before its CLAIM.


def test_floor_fill_alarm_a_lane_that_ended_without_a_claim_alarms_once(
    tmp_path: Path,
) -> None:
    lane, key, started = _real_tick_lane(tmp_path)
    ended_state = json.dumps(
        {
            "seq": 4,
            "entries": {
                key: {
                    "key": key,
                    "state": "SENT",
                    "lane": lane,
                    "attempt": 1,
                    "ticket": "T-1",
                    "ended": True,
                    "reserved_at": None,
                    "sent_at": started.isoformat(),
                }
            },
        }
    )
    source = StoredStateSentLanes({SCOPE: ended_state}.get, SCOPE, lambda: ())
    (ended,) = source.read_sent_lanes()
    assert ended.ended is True
    ledger = _Ledger()
    handler = HandlerFloorFillAlarmCheck(
        runs=_Runs(), lanes=source, pool=_Pool(), ledger=ledger
    )
    # Inside the CLAIM deadline: a lane that died is alarmed now, not at the deadline.
    result = handler.handle(_request(started + timedelta(minutes=1)))
    assert result.alarms_found == 1
    assert "lane ended with no CLAIM" in ledger.held[0]
    handler.handle(_request(started + timedelta(hours=9)))
    assert len(ledger.held) == 1, "the same dead lane is not alarmed on every check"
    # An ended lane that did give its CLAIM is quiet.
    claimed = StoredStateSentLanes(
        {SCOPE: ended_state}.get,
        SCOPE,
        lambda: (_claim(started + timedelta(seconds=30), lane, key),),
    )
    assert claimed.read_sent_lanes() == ()


# Finding 8: a run stamped after the check.


def test_floor_fill_alarm_a_run_stamped_after_now_is_converted_and_logged(
    tmp_path: Path,
) -> None:
    section = alarm_section(tmp_path)
    settings = load_floor_fill_alarm_settings(section)
    ahead = ModelFloorFillRun(
        observed_at=NOW + timedelta(minutes=3), free_slots=25, lanes_sent=0, cause="c"
    )
    answer = ModelFloorFillStoredAnswer(
        lines=("NOTE a | b",),
        status=EnumFloorFillStatus.OK,
        observed_at=ahead.observed_at,
        seq=1,
        state_seq=1,
        free_slots=25,
        lanes_started=0,
    ).model_dump_json()
    store = {SCOPE + ":answer": answer}
    result = run_alarm_check(
        settings, load_payload=store.get, now=NOW, pool_clock=lambda: NOW
    )
    assert result.runs_clamped == 1
    assert result.runs_read == 1
    (line,) = [
        json.loads(text)
        for text in Path(section["check_log_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert line["outcome"] == "checked"
    assert line["runs_clamped"] == 1


def test_floor_fill_alarm_a_check_that_fails_for_any_reason_still_writes_its_line(
    tmp_path: Path,
) -> None:
    section = alarm_section(tmp_path)
    settings = load_floor_fill_alarm_settings(section)
    Path(section["ledger_path"]).unlink()
    with pytest.raises(AlarmSourceError):
        run_alarm_check(
            settings, load_payload=lambda _key: None, now=NOW, pool_clock=lambda: NOW
        )
    (line,) = Path(section["check_log_path"]).read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["outcome"] == "failed"


# Finding 12: the Z stamp.


def test_floor_fill_alarm_the_z_stamp_is_the_utc_time_of_a_check_in_another_zone() -> (
    None
):
    from omnimarket.nodes.node_floor_fill_alarm_compute.handlers.handler_floor_fill_alarm import (
        HandlerFloorFillAlarm,
    )
    from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
        ModelFloorFillAlarmRequest,
    )

    here = datetime(2026, 11, 16, 19, 0, tzinfo=timezone(timedelta(hours=2)))
    answer = HandlerFloorFillAlarm().handle(
        ModelFloorFillAlarmRequest(
            now=here,
            runs=(),
            pool=ModelFloorFillPoolRead(free_slots=None, unread_cause="x"),
        )
    )
    row = render_row(_request(here), answer.alarms[0])
    assert row.startswith("2026-11-16T17:00:00Z | STATUS | ")


# Finding 7: the principal cell.


def test_floor_fill_alarm_the_row_carries_the_principal_as_its_own_cell() -> None:
    from omnimarket.nodes.node_floor_fill_alarm_compute.handlers.handler_floor_fill_alarm import (
        HandlerFloorFillAlarm,
    )
    from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
        ModelFloorFillAlarmRequest,
    )

    answer = HandlerFloorFillAlarm().handle(
        ModelFloorFillAlarmRequest(
            now=NOW,
            runs=(),
            pool=ModelFloorFillPoolRead(free_slots=None, unread_cause="x"),
        )
    )
    cells = render_row(_request(), answer.alarms[0]).split(CELL_SEPARATOR)
    assert f"principal={AUTHENTICATED_PRINCIPAL}" in cells
    assert cells.index("lane=floor-fill-alarm") + 1 == cells.index(
        f"principal={AUTHENTICATED_PRINCIPAL}"
    ), "the writer reads a principal as a header cell before the row text"


# Finding 2: the runtime builds the handler from injectable params alone.


def test_floor_fill_alarm_the_handler_is_built_with_no_ports_and_refuses_by_name() -> (
    None
):
    required = [
        name
        for name, param in inspect.signature(
            HandlerFloorFillAlarmCheck.__init__
        ).parameters.items()
        if name != "self" and param.default is inspect.Parameter.empty
    ]
    assert required == [], f"the runtime cannot build the handler: {required}"
    bare = HandlerFloorFillAlarmCheck()
    with pytest.raises(AlarmSourceError, match="port unbound: runs"):
        bare.handle(_request())


# Finding 2: the fill's own entry runs the check after a live tick.


def run_alarm_after_tick(*args: object, **kwargs: object) -> ModelFloorFillEntryResult:
    from omnimarket.nodes.node_floor_fill_orchestrator import (
        runtime_floor_fill_orchestrator as entry,
    )

    return entry.run_alarm_after_tick(*args, **kwargs)  # type: ignore[arg-type]


def _entry_result(tmp_path: Path) -> tuple[ModelFloorFillEntryResult, dict[str, str]]:
    out, code, store = live_tick(tmp_path, free=2, age_seconds=5)
    rows = {key: store.load(key).payload or "" for key in (SCOPE, SCOPE + ":answer")}
    return ModelFloorFillEntryResult(exit_code=code, stdout=out), rows


def test_floor_fill_alarm_the_fill_entry_runs_the_check_after_a_live_tick(
    tmp_path: Path,
) -> None:
    result, rows = _entry_result(tmp_path)
    section = alarm_section(tmp_path)
    after = run_alarm_after_tick(
        result,
        EnumFloorFillCommand.TICK.value,
        {"floor_fill_alarm": section},
        load_payload=rows.get,
        now=datetime.now(UTC) + timedelta(minutes=15),
        pool_clock=lambda: datetime.now(UTC),
    )
    lines = after.stdout.splitlines()
    assert lines[: len(result.stdout.splitlines())] == result.stdout.splitlines()
    assert lines[-1].startswith("NOTE alarm-check | alarms=")
    assert after.exit_code == result.exit_code
    log = Path(section["check_log_path"]).read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    assert json.loads(log[0])["runs_read"] == 1
    assert "alarm=floor-fill-unclaimed" in Path(section["ledger_path"]).read_text(
        encoding="utf-8"
    )


def test_floor_fill_alarm_the_fill_entry_runs_no_check_without_the_section_or_for_another_command(
    tmp_path: Path,
) -> None:
    result, rows = _entry_result(tmp_path)
    section = alarm_section(tmp_path)
    assert (
        run_alarm_after_tick(result, "tick", {"floor_fill": {}}, load_payload=rows.get)
        == result
    )
    assert (
        run_alarm_after_tick(
            result, "lines", {"floor_fill_alarm": section}, load_payload=rows.get
        )
        == result
    )
    assert not Path(section["check_log_path"]).exists()


def test_floor_fill_alarm_the_fill_entry_names_a_section_it_refuses_and_fails(
    tmp_path: Path,
) -> None:
    result, rows = _entry_result(tmp_path)
    section = alarm_section(tmp_path)
    del section["row_principal"]
    after = run_alarm_after_tick(
        result, "tick", {"floor_fill_alarm": section}, load_payload=rows.get
    )
    assert after.stdout.splitlines()[-1].startswith("UNKNOWN alarm-check:overlay:")
    assert "floor_fill_alarm.row_principal" in after.stdout
    assert after.exit_code == FAILED


def test_floor_fill_alarm_the_fill_entry_fails_on_a_ledger_the_check_cannot_read(
    tmp_path: Path,
) -> None:
    result, rows = _entry_result(tmp_path)
    section = alarm_section(tmp_path)
    Path(section["ledger_path"]).unlink()
    after = run_alarm_after_tick(
        result, "tick", {"floor_fill_alarm": section}, load_payload=rows.get
    )
    assert after.stdout.splitlines()[-1].startswith("UNKNOWN alarm-check:")
    assert after.exit_code == FAILED


# Finding 2: the tick unit carries the alarm section, so the check runs wherever the fill ticks.


def _install(tmp_path: Path, overlay: dict[str, object]) -> tuple[int, str, str]:
    from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
        run_schedule,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import OVERLAY

    root = tmp_path / "registry"
    python = root / ".onex-dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    result = run_schedule(
        [
            "install",
            "--this-host",
            OVERLAY["host_id"],
            "--workspace-root",
            str(root),
            "--dry-run",
            "--profile",
            "live",
        ],
        {"floor_fill": OVERLAY, **overlay},
        hosts=[(OVERLAY["host_id"], True)],
        home=tmp_path,
        run=lambda command: __import__("subprocess").CompletedProcess(
            list(command), 0, "", ""
        ),
    )
    return result.exit_code, result.stdout, result.stderr


def test_floor_fill_alarm_the_tick_unit_carries_the_alarm_section(
    tmp_path: Path,
) -> None:
    import shlex

    from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
        live_section,
    )

    section = alarm_section(tmp_path)
    # A unit never runs from a worktree: the install refuses an interpreter under one, so the command is a host's own.
    section["ledger_append_command"] = ["/srv/example/bin/ledger-append", "{row}"]
    code, out, err = _install(
        tmp_path,
        {
            "floor_fill_live": live_section(tmp_path),
            "floor_fill_alarm": section,
        },
    )
    assert code == 0, err
    (line,) = [
        x
        for x in out.splitlines()
        if x.startswith("ExecStart=") and x.endswith(" tick")
    ]
    words = shlex.split(
        line.removeprefix("ExecStart=").replace("%%", "%").replace("$$", "$")
    )
    carried = json.loads(words[4])["floor_fill_alarm"]
    assert carried["row_principal"] == AUTHENTICATED_PRINCIPAL
    assert carried["scope"] == section["scope"]
    # Without the section the unit is what it was: no check is added to a fill that was not given one.
    code, plain, _ = _install(tmp_path, {"floor_fill_live": live_section(tmp_path)})
    assert code == 0
    (plain_line,) = [x for x in plain.splitlines() if x.startswith("ExecStart=")]
    plain_words = shlex.split(
        plain_line.removeprefix("ExecStart=").replace("%%", "%").replace("$$", "$")
    )
    assert "floor_fill_live" in json.loads(plain_words[4])
    assert "floor_fill_alarm" not in json.loads(plain_words[4])


def test_floor_fill_alarm_the_install_refuses_an_alarm_section_naming_its_missing_key(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
        live_section,
    )

    section = alarm_section(tmp_path)
    del section["row_principal"]
    code, _out, err = _install(
        tmp_path,
        {"floor_fill_live": live_section(tmp_path), "floor_fill_alarm": section},
    )
    assert code == 1
    assert "floor_fill_alarm.row_principal" in err
