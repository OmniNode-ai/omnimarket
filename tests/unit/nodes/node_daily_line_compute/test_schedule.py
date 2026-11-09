# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line's schedule and scheduled run (OMN-22925 AC3).

The timer fires at 05:30 on the Eastern clock; one run reads the floor record and the open claims the bus answers, publishes the line beside
the floor record and writes exactly one ledger STATUS row. A floor record or ledger that cannot be read gives UNKNOWN for
its part and the run still publishes and writes its row.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from omnimarket.nodes.node_daily_line_effect.handlers import schedule as sched
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
    bus_appender,
    count_open_claims,
    load_runtime,
    read_floor_record,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    InMemoryPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 10, 30, tzinfo=UTC)  # 05:30 Eastern, standard time


def _floor(generated_at: datetime, *, reading_state: str | None = "WORKING") -> str:
    return json.dumps(
        {
            "schema_id": "omni.floor-simple/1",
            "generated_at": generated_at.isoformat(),
            "pieces": [
                {
                    "piece": 1,
                    "name": "code graph",
                    "reading_state": reading_state,
                    "text": "2 nightly runs in 24 hours",
                    "joins": [
                        {"id": "J0", "state": "golden", "to_piece": 1},
                        {"id": "J1", "state": "passed", "to_piece": 2},
                    ],
                },
                {
                    "piece": 2,
                    "name": "hosts",
                    "reading_state": None,
                    "text": "",
                    "joins": [],
                },
            ],
        }
    )


class FakeRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        failed = self.fail_on is not None and self.fail_on in command
        return subprocess.CompletedProcess(list(command), 1 if failed else 0, "", "")


def _home(tmp_path: Path, *, floor: str | None) -> Path:
    home = tmp_path / "registry"
    floor_dir = home / ".claude_scratch" / "factory-floor"
    floor_dir.mkdir(parents=True)
    if floor is not None:
        (floor_dir / "floor-simple.json").write_text(floor, encoding="utf-8")
    return home


def _read(open_claims: int | None) -> InMemoryPointRead:
    """The in-memory point read: an answer of ``open_claims`` claims, or no answer at all."""
    if open_claims is None:
        return InMemoryPointRead()
    return InMemoryPointRead({"open-claims": ModelPointAnswer(matched=open_claims)})


def _runner(
    home: Path, read: InMemoryPointRead | None, rows: list[str]
) -> HandlerDailyLineRun:
    return HandlerDailyLineRun(
        load_runtime(),
        home=home,
        ledger=read,
        appender=rows.append,
        clock=lambda: NOW,
    )


class FakeAppender:
    """A bus append that answers with a fixed outcome and records each (rows, request id)."""

    def __init__(
        self, status: Literal["accepted", "duplicate", "refused", "pending", "error"]
    ) -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append((rows, request_id))
        return ModelAppendOutcome(status=self.status, message="fixed")


# --- the schedule ------------------------------------------------------------------------------------------------------


def test_schedule_fires_at_0530_on_the_eastern_clock() -> None:
    schedule = sched.load_schedule()
    _, timer = sched.render_systemd(
        schedule,
        omni_root=Path("/data/omni"),
        home=Path("/home/lab"),
        python=Path("/data/omni/.venv/bin/python"),
    )
    assert "OnCalendar=*-*-* 05:30:00 America/New_York\n" in timer
    assert "Persistent=true\n" in timer
    assert "Unit=onex-daily-line.service\n" in timer


def test_schedule_service_runs_the_node_daily_verb_from_the_registry_root() -> None:
    schedule = sched.load_schedule()
    service, _ = sched.render_systemd(
        schedule,
        omni_root=Path("/data/omni"),
        home=Path("/home/lab"),
        python=Path("/data/omni/.venv/bin/python"),
    )
    assert (
        "ExecStart=/data/omni/.venv/bin/python -m omnimarket.nodes.node_daily_line_effect daily\n"
        in service
    )
    assert 'Environment="OMNI_HOME=/data/omni"\n' in service
    assert "Type=oneshot\n" in service


def test_schedule_refuses_a_path_a_unit_line_cannot_carry() -> None:
    with pytest.raises(ValueError, match="registry root"):
        sched.render_systemd(
            sched.load_schedule(),
            omni_root=Path("/data/omni home"),
            home=Path("/home/lab"),
            python=Path("/usr/bin/python3"),
        )


def test_schedule_install_writes_both_units_and_enables_the_timer(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    code = sched.install(
        sched.load_schedule(),
        omni_root=tmp_path / "omni",
        home=tmp_path / "home",
        python=Path("/usr/bin/python3"),
        runner=runner,
    )
    service, timer = sched.unit_paths(sched.load_schedule(), tmp_path / "home")
    assert code == 0
    assert service.is_file()
    assert "05:30:00 America/New_York" in timer.read_text(encoding="utf-8")
    assert runner.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "onex-daily-line.timer"],
    ]


def test_schedule_uninstall_is_the_rollback_and_removes_both_units(
    tmp_path: Path,
) -> None:
    schedule = sched.load_schedule()
    sched.install(
        schedule,
        omni_root=tmp_path / "omni",
        home=tmp_path / "home",
        python=Path("/usr/bin/python3"),
        runner=FakeRunner(),
    )
    runner = FakeRunner()
    assert sched.uninstall(schedule, home=tmp_path / "home", runner=runner) == 0
    assert not any(
        path.exists() for path in sched.unit_paths(schedule, tmp_path / "home")
    )
    assert runner.calls[0] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-daily-line.timer",
    ]


def test_schedule_install_reports_a_failed_enable(tmp_path: Path) -> None:
    code = sched.install(
        sched.load_schedule(),
        omni_root=tmp_path / "omni",
        home=tmp_path / "home",
        python=Path("/usr/bin/python3"),
        runner=FakeRunner(fail_on="enable"),
    )
    assert code == 1


# --- the scheduled run -------------------------------------------------------------------------------------------------


def test_schedule_run_publishes_the_line_and_writes_one_status_row(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, floor=_floor(NOW - timedelta(minutes=4)))
    rows: list[str] = []
    runner = _runner(home, _read(2), rows)

    publication = runner.run()

    expected = (
        "Piece 1, code graph: WORKING (2 nightly runs in 24 hours), joined to piece 2, hosts, "
        "by J1: passed; claims with no TERMINAL: 2."
    )
    assert publication.line == expected
    published = json.loads(runner.published_path.read_text(encoding="utf-8"))
    assert published["schema_id"] == "omni.daily-line/1"
    assert published["line"] == expected
    assert runner.published_path.parent == home / ".claude_scratch" / "factory-floor"
    assert len(rows) == 1
    assert rows[0].startswith(
        "2026-11-08T10:30:00Z | STATUS | lane=node_daily_line_effect | ticket=OMN-22925 | "
    )
    assert (
        "actor=script:node_daily_line_effect | model=none | unknown=none | " in rows[0]
    )
    assert rows[0].endswith(expected)


def test_schedule_run_with_every_source_missing_still_publishes_and_writes_one_row(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, floor=None)
    rows: list[str] = []
    runner = _runner(home, _read(None), rows)

    publication = runner.run()

    assert publication.unknown_parts == ("piece1", "j1", "claims")
    assert json.loads(runner.published_path.read_text(encoding="utf-8"))[
        "unknown_parts"
    ] == [
        "piece1",
        "j1",
        "claims",
    ]
    assert len(rows) == 1
    assert "unknown=piece1,j1,claims | " in rows[0]
    assert "claims with no TERMINAL: UNKNOWN." in rows[0]


def test_schedule_run_a_stale_floor_record_is_not_a_reading(tmp_path: Path) -> None:
    home = _home(tmp_path, floor=_floor(NOW - timedelta(hours=3)))
    rows: list[str] = []
    publication = _runner(home, _read(2), rows).run()
    assert publication.unknown_parts == ("piece1", "j1")
    assert publication.parts[2].text == "2"


def test_schedule_run_a_floor_piece_with_no_reading_state_reads_unknown_with_the_floors_words(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, floor=_floor(NOW, reading_state=None))
    publication = _runner(home, _read(2), []).run()
    assert publication.line.startswith(
        "Piece 1, code graph: UNKNOWN (2 nightly runs in 24 hours), joined to piece 2, hosts, by J1: passed;"
    )


@pytest.mark.parametrize(
    "text", ["not json", "[]", '{"generated_at": "x"}', '{"pieces": []}']
)
def test_schedule_floor_record_that_is_not_a_floor_reads_as_nothing(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / "floor-simple.json"
    path.write_text(text, encoding="utf-8")
    assert read_floor_record(path, piece=1, now=NOW, max_age_seconds=1800) == (
        None,
        None,
    )


def test_schedule_open_claims_count_is_the_bus_answers_matched_claims() -> None:
    read = _read(2)
    assert count_open_claims(read) == 2
    assert [q.query for q in read.requests] == ["open-claims"]


def test_schedule_open_claims_a_bus_with_no_answer_is_none_not_zero() -> None:
    assert count_open_claims(_read(None)) is None
    assert count_open_claims(None) is None


def test_schedule_open_claims_zero_claims_is_zero_not_unread() -> None:
    assert count_open_claims(_read(0)) == 0


def test_schedule_bus_appender_sends_the_row_whole_under_a_fresh_request_id() -> None:
    appender = FakeAppender("accepted")
    ids = iter(["id-1", "id-2"])
    append = bus_appender(appender, new_id=lambda: next(ids))
    append("a | STATUS | x; rm -rf /")
    append("b | STATUS | y")
    assert appender.calls == [
        ("a | STATUS | x; rm -rf /", "id-1"),
        ("b | STATUS | y", "id-2"),
    ]


@pytest.mark.parametrize("status", ["refused", "pending", "error"])
def test_schedule_bus_appender_raises_unless_the_row_is_accepted(
    status: Literal["refused", "pending", "error"],
) -> None:
    with pytest.raises(RuntimeError, match=f"was {status}: fixed"):
        bus_appender(FakeAppender(status))("row")


def test_schedule_bus_appender_treats_a_duplicate_receipt_as_landed() -> None:
    bus_appender(FakeAppender("duplicate"))("row")


def test_schedule_unit_carries_the_bus_routes_set_at_install_and_skips_the_unset() -> (
    None
):
    service, _ = sched.render_systemd(
        sched.load_schedule(),
        omni_root=Path("/data/omni"),
        home=Path("/home/lab"),
        python=Path("/usr/bin/python3"),
        environment={
            "ONEX_LEDGER_BUS_READ_COMMAND": "/venv/bin/python -m bus read --bus-lane dev",
            "ONEX_HOST_NAME": "  ",
        },
    )
    assert (
        'Environment="ONEX_LEDGER_BUS_READ_COMMAND=/venv/bin/python -m bus read --bus-lane dev"\n'
        in service
    )
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND" not in service
    assert "ONEX_HOST_NAME" not in service


def test_schedule_unit_refuses_a_route_value_a_quoted_line_cannot_carry() -> None:
    with pytest.raises(ValueError, match="ONEX_LEDGER_BUS_READ_COMMAND"):
        sched.render_systemd(
            sched.load_schedule(),
            omni_root=Path("/data/omni"),
            home=Path("/home/lab"),
            python=Path("/usr/bin/python3"),
            environment={"ONEX_LEDGER_BUS_READ_COMMAND": 'bus "read'},
        )
