# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lines`` operation of the factory floor effect (OMN-22371, lean line board plan T6).

Each T2 case is run through the effect to get its record; the record is rendered and compared with the golden text
stored beside this file. A subprocess run of the command line against a temporary home that holds a record prints the
same golden. A stale or absent record prints UNREAD first and exits non-zero.

The goldens show the board of a record built by the effect as it is today: the eight source health entries print as
unread ("the record does not list this source") and every line says its stops are not in the record, until OMN-22368
and OMN-22370 put health and stops in the record. They are regenerated, never edited by hand, when those land.
The unread-sources case (s7, observed) is refused by the effect until OMN-22368, so it has no golden yet.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.handlers.handler_floor_lines import (
    OWN_CONTRACT,
    HandlerFloorLines,
    load_lines_config,
    newest_snapshot_at,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_floor_lines import (
    ModelFloorLinesRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import SourceRefusedError
from tests.nodes.node_factory_floor_compute.line_board_support import (
    CASES,
    VARIANTS,
    run_board,
)

GOLDENS = Path(__file__).parent / "goldens"
# Waiting for the effect to accept an all-sources-unread home (OMN-22368).
NO_GOLDEN_YET = {("s7_all_sources_unread", "observed")}
RUN_AFTER_RECORD = timedelta(minutes=4)

CASE_PARAMS = [
    pytest.param(case, variant, id=f"{case}-{variant}")
    for case in CASES
    for variant in VARIANTS
    if (case, variant) not in NO_GOLDEN_YET
]


def _golden(case: str, variant: str) -> str:
    return (GOLDENS / f"{case}-{variant}.txt").read_text(encoding="utf-8")


def _run_time(record: dict[str, object]) -> datetime:
    return (
        datetime.fromisoformat(str(record["generated_at"]).replace("Z", "+00:00"))
        + RUN_AFTER_RECORD
    )


def _run_lines(record_path: Path, now: datetime) -> tuple[str, int]:
    result = HandlerFloorLines().handle(
        ModelFloorLinesRequest(record_path=str(record_path), now=now)
    )
    return result.board, result.exit_status


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), CASE_PARAMS)
def test_golden_chain_lines_render_equals_the_recorded_golden(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = run_board(case, variant, tmp_path, monkeypatch)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    board, status = _run_lines(path, _run_time(record))
    assert board == _golden(case, variant)
    assert status == 0


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), CASE_PARAMS[:3])
def test_golden_chain_lines_subprocess_prints_the_golden(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command line against a temporary home holding a fixture record prints the golden."""
    record = run_board(case, variant, tmp_path, monkeypatch)
    home = tmp_path / "lines_home"
    relpath = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))["runtime"][
        "output"
    ]["relpath"]
    (home / relpath).parent.mkdir(parents=True)
    (home / relpath).write_text(json.dumps(record), encoding="utf-8")
    now = _run_time(record).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_factory_floor_effect",
            "lines",
            "--home",
            str(home),
            "--now",
            now,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == _golden(case, variant)


@pytest.mark.unit
def test_error_chain_lines_stale_record_prints_unread_first_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = run_board(CASES[0], VARIANTS[0], tmp_path, monkeypatch)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cfg = load_lines_config()
    late = _run_time(record) + timedelta(minutes=cfg.record_max_age_min)
    board, status = _run_lines(path, late)
    assert board.splitlines()[0].startswith("UNREAD: record ")
    assert "BOARD record" in board
    assert status == cfg.unread_exit_status != 0
    fresh_board, fresh_status = _run_lines(path, _run_time(record))
    assert not fresh_board.startswith("UNREAD")
    assert fresh_status == 0


@pytest.mark.unit
def test_error_chain_lines_absent_record_prints_unread_only_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    board, status = _run_lines(tmp_path / "none.json", datetime.now(UTC))
    assert board.startswith("UNREAD: record absent")
    assert board.count("\n") == 1
    assert status == load_lines_config().unread_exit_status != 0


@pytest.mark.unit
def test_error_chain_lines_unparsable_record_prints_unread_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    path = tmp_path / "record.json"
    path.write_text("{not json", encoding="utf-8")
    board, status = _run_lines(path, datetime.now(UTC))
    assert board.startswith("UNREAD: record not readable")
    assert status != 0


@pytest.mark.unit
def test_lines_subprocess_stale_record_exits_non_zero(tmp_path: Path) -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_factory_floor_effect",
            "lines",
            "--home",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == load_lines_config().unread_exit_status
    assert done.stdout.startswith("UNREAD: record absent")


@pytest.mark.unit
def test_lines_reads_a_named_history_snapshot(tmp_path: Path) -> None:
    cfg = load_lines_config()
    history = tmp_path / cfg.history_subdirectory
    history.mkdir()
    times = (
        "20261108T100000000000Z",
        "20261108T110000000000Z",
        "20261108T120000000000Z",
    )
    for stamp in times:
        moment = datetime.strptime(stamp, cfg.history_timestamp_format).replace(
            tzinfo=UTC
        )
        (history / f"factory-floor-{stamp}.json").write_text(
            json.dumps({"generated_at": moment.isoformat(), "lines": []}),
            encoding="utf-8",
        )
    at = datetime(2026, 11, 8, 11, 5, tzinfo=UTC)
    found = newest_snapshot_at(history, at, cfg)
    assert found is not None
    assert found.name == f"factory-floor-{times[1]}.json"
    assert (
        newest_snapshot_at(history, datetime(2026, 11, 8, 9, tzinfo=UTC), cfg) is None
    )
    result = HandlerFloorLines().handle(
        ModelFloorLinesRequest(history_at=at, history_dir=str(tmp_path), now=at)
    )
    assert result.record_time == "2026-11-08T11:00:00Z"
    assert result.exit_status == 0
    # A gap in the publish history is not hidden: the same snapshot half an hour on reads stale.
    gap = HandlerFloorLines().handle(
        ModelFloorLinesRequest(
            history_at=at, history_dir=str(tmp_path), now=at + timedelta(minutes=25)
        )
    )
    assert gap.exit_status != 0


@pytest.mark.unit
def test_lines_prints_health_and_stops_when_the_record_carries_them(
    tmp_path: Path,
) -> None:
    record = {
        "generated_at": "2026-11-08T12:00:00Z",
        "sources": {
            "health": [
                {
                    "source": "plans",
                    "state": "read",
                    "read_at": "2026-11-08T11:59:00Z",
                    "cause": "",
                },
                {
                    "source": "watcher",
                    "state": "stale",
                    "read_at": "2026-11-08T08:00:00Z",
                    "cause": "older than 90 min",
                },
                {
                    "source": "goal",
                    "state": "unread",
                    "read_at": None,
                    "cause": "no such file",
                },
            ]
        },
        "lines": [
            {
                "line_id": "ticket:OMN-1",
                "name": "unplanned work",
                "condition": "stopped",
                "owner": "lane-a",
                "ruled_by": ["ruling:x"],
                "stage_counts": {"planned": 1, "merged": 2},
                "stops": [
                    {
                        "kind": "merged-not-running",
                        "sentence": "merged and not running",
                        "since": "2026-11-08T09:00:00Z",
                        "evidence": [{"kind": "merge", "ref": "repo#1"}],
                        "cause": "",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    board, status = _run_lines(path, datetime(2026, 11, 8, 12, 2, tzinfo=UTC))
    assert status == 0
    rows = board.splitlines()
    assert rows[:3] == [
        "BOARD record 2026-11-08T12:00:00Z, 2 min old",
        "SOURCES",
        "  plans: read 2026-11-08T11:59:00Z",
    ]
    assert "  watcher: stale 2026-11-08T08:00:00Z - older than 90 min" in rows
    assert "  goal: unread - no such file" in rows
    assert "  ledger: unread - the record does not list this source" in rows
    assert (
        "    stages: planned 1, building 0, pr-open 0, merged 2, running 0, unread 0"
        in rows
    )
    assert (
        "    stop merged-not-running: merged and not running (since 2026-11-08T09:00:00Z)"
        in rows
    )
    assert "      cause: cause not recorded: run lean_five_whys" in rows
    assert "      evidence: merge repo#1" in rows


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "record_max_age_min",
        "health_sources",
        "stage_order",
        "words",
        "unread_exit_status",
        "history_dir_env",
    ],
)
def test_lines_config_fails_closed_naming_the_missing_key(
    key: str, tmp_path: Path
) -> None:
    contract = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))
    del contract["lines"][key]
    broken = tmp_path / "contract.yaml"
    broken.write_text(yaml.safe_dump(contract), encoding="utf-8")
    with pytest.raises(SourceRefusedError, match=key):
        load_lines_config(broken)


@pytest.mark.unit
def test_lines_reads_no_source_but_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the record the only file under the home, the board still renders: the operation opens nothing else there."""
    record = {"generated_at": "2026-11-08T12:00:00Z", "lines": []}
    relpath = yaml.safe_load(OWN_CONTRACT.read_text(encoding="utf-8"))["runtime"][
        "output"
    ]["relpath"]
    (tmp_path / relpath).parent.mkdir(parents=True)
    (tmp_path / relpath).write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    result = HandlerFloorLines().handle(
        ModelFloorLinesRequest(now=datetime(2026, 11, 8, 12, 1, tzinfo=UTC))
    )
    assert result.exit_status == 0
    assert result.record_time == "2026-11-08T12:00:00Z"
