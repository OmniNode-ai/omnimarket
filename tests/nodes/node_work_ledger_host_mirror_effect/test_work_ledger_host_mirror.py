# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_ledger_host_mirror_effect: a ruling written on the ledger of record reaches every lab host in 300 s (OMN-25537).

The first block is the reading the lane recorded on 2026-11-18 (read at about 01:19Z, ledger of record newest row
01:18:26Z): on each of four lab hosts the ledger file in the registry clone stopped at a stamp hours to weeks behind the
record, and the newest RULING any of them held was 2026-10-13T22:42:38Z. The declared sync of those hosts was the hourly
clone fast-forward (h201, h202) or none that carries the file (h051, h053). The tests hold the declared replacement to
the 300 s bound the reading broke.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.adapter_bus_read import (
    BusLedgerRowsReader,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.handler_work_ledger_host_mirror import (
    HandlerWorkLedgerHostMirror,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_work_ledger_host_mirror import (
    EnumHostMirrorStatus,
    ModelHostMirrorRequest,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.protocols import (
    LedgerReadError,
    ModelLedgerRow,
    ModelLedgerRowsPage,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    Recorder,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

UNIT = "ledger-host-mirror-systemd"
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_ledger_host_mirror_effect"
)
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
BOUND_SECONDS = 300
RECORD_NEWEST = datetime(2026, 11, 18, 1, 18, 26, tzinfo=UTC)


@dataclass(frozen=True)
class Reading:
    """One host's ledger copy as read, with the sync declared for it."""

    host: str
    newest_row: datetime
    newest_ruling: datetime
    sync_path: str
    sync_period_seconds: int | None  # None: no declared sync carries the ledger file


NEWEST_RULING = datetime(2026, 10, 13, 22, 42, 38, tzinfo=UTC)
RECORDED = (
    Reading(
        "h051",
        datetime(2026, 11, 18, 0, 9, 23, tzinfo=UTC),
        NEWEST_RULING,
        "canonical-clone-sync (code clones only; the ledger file is host-local hook rows)",
        None,
    ),
    Reading(
        "h053",
        datetime(2026, 11, 17, 21, 25, 52, tzinfo=UTC),
        NEWEST_RULING,
        "canonical-clone-sync + internal-clone-sync (hourly :27); the ledger file is host-local hook rows",
        None,
    ),
    Reading(
        "h201",
        datetime(2026, 11, 17, 23, 14, 23, tzinfo=UTC),
        NEWEST_RULING,
        "omni-home-ff hourly :17 (tracked files only)",
        3600,
    ),
    Reading(
        "h202",
        datetime(2026, 10, 14, 0, 8, 26, tzinfo=UTC),
        NEWEST_RULING,
        "omni-home-ff hourly :17 (tracked files only)",
        3600,
    ),
)


def declared_worst_case_seconds() -> int:
    """The longest a row waits to reach a host: tick period, the run limit and the projection lag the contract declares."""
    unit = load_units()[UNIT]
    delivery = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["delivery"]
    schedule = unit["schedule"]
    assert schedule["start_interval"] == delivery["tick_period_seconds"]
    assert schedule["run_timeout_s"] == delivery["tick_run_seconds"]
    return (
        schedule["start_interval"]
        + schedule["run_timeout_s"]
        + delivery["projection_lag_seconds"]
    )


@pytest.mark.parametrize("reading", RECORDED, ids=lambda r: r.host)
def test_recorded_reading_broke_the_bound_and_the_declared_unit_holds_it(
    reading: Reading,
) -> None:
    lag = int((RECORD_NEWEST - reading.newest_row).total_seconds())
    assert lag > BOUND_SECONDS, f"{reading.host} was {lag} s behind the record"
    assert int((RECORD_NEWEST - reading.newest_ruling).total_seconds()) > BOUND_SECONDS
    assert (
        reading.sync_period_seconds is None
        or reading.sync_period_seconds > BOUND_SECONDS
    )
    assert UNIT in load_units(), (
        f"no declared unit delivers the ledger to {reading.host}"
    )
    assert load_units()[UNIT]["platform"] == "systemd"
    assert declared_worst_case_seconds() <= BOUND_SECONDS


def test_contract_delivery_bound_is_the_bound() -> None:
    delivery = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["delivery"]
    assert delivery["max_seconds"] == BOUND_SECONDS
    assert declared_worst_case_seconds() <= delivery["max_seconds"]


def test_golden_the_unit_renders_a_two_minute_timer_and_names_the_read_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == "/usr/bin/python3.12"
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            ledger_host_name="h051",
        )
    )
    assert result.status == "success", result.stderr
    assert "OnUnitActiveSec=120" in result.rendered
    assert "ONEX_LEDGER_BUS_READ_COMMAND=" in result.rendered
    assert "ledger query --stdin" in result.rendered
    assert "ONEX_WORKSPACE_CONFIG_ROOT=" in result.rendered
    assert unit["module"] in result.rendered


# --- the handler -----------------------------------------------------------------------------------------


def _row(
    second: int, ident: str | None = None, text: str | None = None
) -> ModelLedgerRow:
    stamp = datetime(2026, 11, 18, 1, 0, 0, tzinfo=UTC) + timedelta(seconds=second)
    return ModelLedgerRow(
        row_id=ident or f"row-{second}",
        row_ts=stamp,
        raw_row=text or f"{stamp:%Y-%m-%dT%H:%M:%SZ} | RULING | lane=t | row {second}",
    )


class FakeReader:
    """The record: every row it holds, paged oldest first from ``since``."""

    def __init__(self, rows: list[ModelLedgerRow]) -> None:
        self.rows = rows
        self.calls: list[tuple[datetime, str | None]] = []
        self.fail = False

    def read_rows(
        self, *, since: datetime, cursor: str | None, page_size: int
    ) -> ModelLedgerRowsPage:
        self.calls.append((since, cursor))
        if self.fail:
            raise LedgerReadError("read node refused")
        held = sorted(
            (r for r in self.rows if r.row_ts >= since), key=lambda r: r.row_ts
        )
        start = int(cursor or 0)
        chunk = held[start : start + page_size]
        more = start + page_size < len(held)
        newest = max((r.row_ts for r in self.rows), default=None)
        return ModelLedgerRowsPage(
            rows=tuple(chunk),
            next_cursor=str(start + page_size) if more else None,
            record_newest_row_ts=newest,
        )


def _request(tmp_path: Path, **kw: object) -> ModelHostMirrorRequest:
    fields: dict[str, object] = {
        "mirror_path": tmp_path / "mirror" / "host-ledger-mirror.md",
        "state_path": tmp_path / "state" / "mirror-state.json",
        "now": datetime(2026, 11, 18, 1, 1, 0, tzinfo=UTC),
        "window_seconds": 3600,
        "overlap_seconds": 60,
        "page_size": 2,
    }
    fields.update(kw)
    return ModelHostMirrorRequest.model_validate(fields)


def _lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "mirror" / "host-ledger-mirror.md").read_text().splitlines()


def test_golden_a_new_row_on_the_record_is_on_the_mirror_after_the_next_tick(
    tmp_path: Path,
) -> None:
    reader = FakeReader([_row(1), _row(2), _row(3)])
    handler = HandlerWorkLedgerHostMirror(reader)
    first = handler.handle(_request(tmp_path))
    assert first.status is EnumHostMirrorStatus.MIRRORED
    assert first.rows_appended == 3
    assert first.pages == 2
    assert [ln.split(" | ")[0][-9:] for ln in _lines(tmp_path)] == [
        "01:00:01Z",
        "01:00:02Z",
        "01:00:03Z",
    ]
    reader.rows.append(
        _row(70, text="2026-11-18T01:01:10Z | RULING | lane=t | the new ruling")
    )
    second = handler.handle(
        _request(tmp_path, now=datetime(2026, 11, 18, 1, 2, 0, tzinfo=UTC))
    )
    assert second.rows_appended == 1
    assert _lines(tmp_path)[-1].endswith("the new ruling")
    assert second.lag_seconds == 0
    assert len(_lines(tmp_path)) == 4


def test_golden_a_row_read_twice_through_the_overlap_is_appended_once(
    tmp_path: Path,
) -> None:
    reader = FakeReader([_row(1), _row(50)])
    handler = HandlerWorkLedgerHostMirror(reader)
    handler.handle(_request(tmp_path))
    again = handler.handle(_request(tmp_path))
    assert again.rows_appended == 0
    assert len(_lines(tmp_path)) == 2


def test_error_a_refused_read_changes_nothing_and_says_why(tmp_path: Path) -> None:
    reader = FakeReader([_row(1)])
    handler = HandlerWorkLedgerHostMirror(reader)
    handler.handle(_request(tmp_path))
    before = (tmp_path / "mirror" / "host-ledger-mirror.md").read_bytes()
    state_before = (tmp_path / "state" / "mirror-state.json").read_bytes()
    reader.fail = True
    reader.rows.append(_row(5))
    refused = handler.handle(_request(tmp_path))
    assert refused.status is EnumHostMirrorStatus.REFUSED
    assert "read node refused" in refused.detail
    assert (tmp_path / "mirror" / "host-ledger-mirror.md").read_bytes() == before
    assert (tmp_path / "state" / "mirror-state.json").read_bytes() == state_before
    reader.fail = False
    assert handler.handle(_request(tmp_path)).rows_appended == 1


def test_error_a_tick_that_died_after_its_append_is_cut_back_and_never_duplicates(
    tmp_path: Path,
) -> None:
    reader = FakeReader([_row(1), _row(2)])
    handler = HandlerWorkLedgerHostMirror(reader)
    handler.handle(_request(tmp_path))
    mirror = tmp_path / "mirror" / "host-ledger-mirror.md"
    with mirror.open("ab") as held:
        held.write(
            b"2026-11-18T01:00:09Z | RULING | lane=t | appended, state never written\n"
        )
    reader.rows.append(
        _row(
            9,
            text="2026-11-18T01:00:09Z | RULING | lane=t | appended, state never written",
        )
    )
    handler.handle(_request(tmp_path))
    assert [ln for ln in _lines(tmp_path) if "state never written" in ln] == [
        "2026-11-18T01:00:09Z | RULING | lane=t | appended, state never written"
    ]


def test_error_a_mirror_shorter_than_its_state_is_rebuilt_from_the_window(
    tmp_path: Path,
) -> None:
    reader = FakeReader([_row(1), _row(2)])
    handler = HandlerWorkLedgerHostMirror(reader)
    handler.handle(_request(tmp_path))
    (tmp_path / "mirror" / "host-ledger-mirror.md").write_bytes(b"")
    rebuilt = handler.handle(_request(tmp_path))
    assert rebuilt.reset is True
    assert rebuilt.rows_appended == 2
    assert len(_lines(tmp_path)) == 2


def test_error_a_read_that_fails_after_a_page_keeps_that_page_and_resumes(
    tmp_path: Path,
) -> None:
    class FailsOnSecondPage(FakeReader):
        def read_rows(
            self, *, since: datetime, cursor: str | None, page_size: int
        ) -> ModelLedgerRowsPage:
            if cursor is not None:
                raise LedgerReadError("run limit ended the second read")
            return super().read_rows(since=since, cursor=cursor, page_size=page_size)

    rows = [_row(i) for i in range(1, 5)]
    handler = HandlerWorkLedgerHostMirror(FailsOnSecondPage(rows))
    failed = handler.handle(_request(tmp_path))
    assert failed.status is EnumHostMirrorStatus.REFUSED
    assert failed.rows_appended == 2
    assert len(_lines(tmp_path)) == 2
    resumed = HandlerWorkLedgerHostMirror(FakeReader(rows)).handle(_request(tmp_path))
    assert resumed.status is EnumHostMirrorStatus.MIRRORED
    assert len(_lines(tmp_path)) == 4


def test_golden_the_page_bound_ends_a_tick_partial_and_the_next_one_resumes(
    tmp_path: Path,
) -> None:
    reader = FakeReader([_row(i) for i in range(1, 7)])
    handler = HandlerWorkLedgerHostMirror(reader)
    partial = handler.handle(_request(tmp_path, max_pages=1))
    assert partial.status is EnumHostMirrorStatus.PARTIAL
    assert partial.rows_appended == 2
    rest = handler.handle(_request(tmp_path, max_pages=10))
    assert rest.status is EnumHostMirrorStatus.MIRRORED
    assert len(_lines(tmp_path)) == 6


# --- the adapter and the tick -----------------------------------------------------------------------------


def _answer(rows: list[dict[str, str]], **extra: object) -> str:
    return json.dumps(
        {
            "status": "ok",
            "error": None,
            "refusal": None,
            "rows": rows,
            "next_cursor": None,
            "freshness": {"newest_row_ts": "2026-11-18T01:00:05Z"},
            **extra,
        }
    )


def _command(tmp_path: Path, body: str, exit_code: int = 0) -> list[str]:
    script = tmp_path / "read.py"
    script.write_text(
        "import sys, json\n"
        "request = json.loads(sys.stdin.read())\n"
        "assert request['query'] == 'rows' and request['correlation_id']\n"
        f"sys.stdout.write({body!r})\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


ROW = {
    "row_id": "a",
    "row_ts": "2026-11-18T01:00:05Z",
    "raw_row": "2026-11-18T01:00:05Z | MSG | x",
}


def test_golden_the_adapter_asks_for_the_declared_row_kinds(tmp_path: Path) -> None:
    script = tmp_path / "read.py"
    script.write_text(
        "import sys, json\n"
        "request = json.loads(sys.stdin.read())\n"
        "assert request['kinds'] == ['RULING'] and request['page_size'] == 5\n"
        f"sys.stdout.write({_answer([ROW])!r})\n"
    )
    page = BusLedgerRowsReader(
        [sys.executable, str(script)], 20, ("RULING",)
    ).read_rows(since=datetime(2026, 11, 18, tzinfo=UTC), cursor=None, page_size=5)
    assert [r.row_id for r in page.rows] == ["a"]


def test_golden_the_adapter_reads_one_page_through_the_command(tmp_path: Path) -> None:
    page = BusLedgerRowsReader(_command(tmp_path, _answer([ROW])), 20).read_rows(
        since=datetime(2026, 11, 18, tzinfo=UTC), cursor=None, page_size=5
    )
    assert [r.row_id for r in page.rows] == ["a"]
    assert page.record_newest_row_ts == datetime(2026, 11, 18, 1, 0, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("body", "exit_code", "match"),
    [
        (_answer([ROW]), 4, "exited 4"),
        ("not json", 0, "not JSON"),
        (_answer([ROW], status="error"), 0, "status="),
        (_answer([ROW], error="db down"), 0, "db down"),
        (json.dumps({"status": "ok", "rows": [{"row_id": "a"}]}), 0, "malformed"),
    ],
)
def test_error_the_adapter_never_turns_a_failed_read_into_an_empty_page(
    tmp_path: Path, body: str, exit_code: int, match: str
) -> None:
    with pytest.raises(LedgerReadError, match=match):
        BusLedgerRowsReader(_command(tmp_path, body, exit_code), 20).read_rows(
            since=datetime(2026, 11, 18, tzinfo=UTC), cursor=None, page_size=5
        )


def _tick(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_work_ledger_host_mirror_effect.runtime_work_ledger_host_mirror_tick",
            "--omni-home",
            str(tmp_path / "home"),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ":".join(sys.path), **env},
    )


def test_golden_the_tick_writes_the_mirror_and_a_receipt_with_the_lag(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path, _answer([ROW]))
    # The host's tracked archive rolls carry the rulings the briefs cite (the pinned stream, OMN-26412).
    rolls = tmp_path / "home/docs/tracking/archive"
    rolls.mkdir(parents=True)
    (rolls / "x-split.md").write_text(
        "2026-09-29T06:34:17Z | RULING | lane=orchestrator-83 | the landing ruling\n"
    )
    done = _tick(tmp_path, {"ONEX_LEDGER_BUS_READ_COMMAND": " ".join(command)})
    assert done.returncode == 0, done.stderr
    mirror = tmp_path / "home/.onex_state/ledger-mirror/host-ledger-mirror.md"
    assert mirror.read_text() == ROW["raw_row"] + "\n"
    receipt = json.loads((tmp_path / "state/last-run.json").read_text())
    assert receipt["exit_code"] == 0
    assert receipt["results"]["host-ledger-mirror"]["lag_seconds"] == 0
    assert (tmp_path / "home/.onex_state/ledger-mirror/host-ledger-rulings.md").exists()


def test_error_the_tick_with_no_read_command_is_indeterminate_and_writes_no_mirror(
    tmp_path: Path,
) -> None:
    done = _tick(tmp_path, {})
    assert done.returncode == 3
    assert "INDETERMINATE" in done.stderr
    assert not (tmp_path / "home/.onex_state/ledger-mirror").exists()
    assert json.loads((tmp_path / "state/last-run.json").read_text())["exit_code"] == 3


def test_golden_the_tick_answers_its_probe_without_a_read_or_a_write(
    tmp_path: Path,
) -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_work_ledger_host_mirror_effect.runtime_work_ledger_host_mirror_tick",
            "--help",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0
    assert "--omni-home" in done.stdout
    assert textwrap.dedent(done.stderr).strip() == ""
