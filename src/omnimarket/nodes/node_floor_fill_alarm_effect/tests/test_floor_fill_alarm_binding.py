# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm check's four ports are bound, and one bound check over a real fill tick writes the alarm rows.

The check here is built the way the binding builds it (``build_live_alarm_check`` from the overlay's ``floor_fill_alarm``
section), over the rows and the pool snapshot a real live-profile tick left behind, and appends through the ledger's row
grammar. The scheduler that calls it belongs to the Temporal workflow of the floor fill: nothing here starts a timer.

Failing controls: with the pool snapshot stale the bound pool port answers unread (an alarm of its own, never 0 free); a
settings section without ``max_run_age_minutes`` is refused naming the key; a fill with no run at all alarms NOT RUNNING.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_alarm_effect.container.container_live import (
    run_alarm_check,
    store_payload_loader,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.handler_floor_fill_alarm_check import (
    CELL_SEPARATOR,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    FloorFillAlarmConfigError,
    load_floor_fill_alarm_settings,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_ledger_grammar import (
    GRAMMAR_APPENDER,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    SCOPE,
    SETTINGS_MAX_AGE_SECONDS,
    live_tick,
)

pytestmark = pytest.mark.unit

MAX_RUN_AGE_MINUTES = 30


def _section(tmp_path: Path) -> dict[str, Any]:
    appender = tmp_path / "grammar_append.py"
    appender.write_text(GRAMMAR_APPENDER, encoding="utf-8")
    ledger = tmp_path / "alarm-ledger.md"
    ledger.write_text("", encoding="utf-8")
    return {
        "scope": SCOPE,
        "row_lane": "floor-fill-alarm",
        "row_ticket": "OMN-24873",
        "ledger_path": str(ledger),
        "ledger_append_command": [sys.executable, str(appender), str(ledger), "{row}"],
        "run_log_path": str(tmp_path / "alarm" / "runs.jsonl"),
        "check_log_path": str(tmp_path / "alarm" / "checks.jsonl"),
        "pool_snapshot_path": str(tmp_path / "pool.json"),
        "pool_record_suffix": ".read-at.json",
        "pool_snapshot_max_age_seconds": SETTINGS_MAX_AGE_SECONDS,
        "pool_engine": "claude",
        "max_run_age_minutes": MAX_RUN_AGE_MINUTES,
    }


def test_floor_fill_alarm_binding_writes_the_unclaimed_rows_over_a_real_tick(
    tmp_path: Path,
) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir()
    _out, _code, store = live_tick(tick_dir, free=2, age_seconds=5)
    section = _section(tick_dir)
    settings = load_floor_fill_alarm_settings(section)
    tick_at = datetime.now(UTC)
    at = tick_at + timedelta(minutes=15)

    result = run_alarm_check(
        settings,
        load_payload=store_payload_loader(store),
        now=at,
        pool_clock=lambda: tick_at,
    )
    assert result.runs_read == 1
    assert result.lanes_read == 2
    assert result.pool_free_slots == 2
    assert result.alarms_found == 2
    assert len(result.rows_written) == 2
    written = Path(section["ledger_path"]).read_text(encoding="utf-8").splitlines()
    assert written == list(result.rows_written)
    for row in written:
        cells = row.split(CELL_SEPARATOR)
        assert cells[1] == "STATUS"
        assert "alarm=floor-fill-unclaimed" in cells
        assert "lane=floor-fill-alarm" in cells
        assert "actor=script:floor-fill-alarm-check" in cells
    again = run_alarm_check(
        settings,
        load_payload=store_payload_loader(store),
        now=at + timedelta(minutes=1),
        pool_clock=lambda: tick_at,
    )
    assert again.rows_written == ()
    assert again.rows_suppressed == 2


def test_floor_fill_alarm_binding_an_unread_pool_alarms_instead_of_reading_zero(
    tmp_path: Path,
) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir()
    _out, _code, store = live_tick(tick_dir, free=2, age_seconds=5)
    section = _section(tick_dir)
    settings = load_floor_fill_alarm_settings(section)
    tick_at = datetime.now(UTC)
    result = run_alarm_check(
        settings,
        load_payload=store_payload_loader(store),
        now=tick_at + timedelta(minutes=1),
        pool_clock=lambda: tick_at + timedelta(seconds=SETTINGS_MAX_AGE_SECONDS * 4),
    )
    assert result.pool_free_slots is None
    rows = Path(section["ledger_path"]).read_text(encoding="utf-8")
    assert "alarm=floor-fill-pool-unread" in rows


def test_floor_fill_alarm_binding_a_fill_with_no_run_alarms_not_running(
    tmp_path: Path,
) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir()
    live_tick(tick_dir, free=2, age_seconds=5)
    section = _section(tick_dir)
    settings = load_floor_fill_alarm_settings(section)
    tick_at = datetime.now(UTC)
    result = run_alarm_check(
        settings,
        load_payload=lambda _key: None,
        now=tick_at,
        pool_clock=lambda: tick_at,
    )
    assert result.alarms_found == 1
    rows = Path(section["ledger_path"]).read_text(encoding="utf-8")
    assert "alarm=floor-fill-not-running" in rows
    assert f"in the last {MAX_RUN_AGE_MINUTES} minutes" in rows


def test_floor_fill_alarm_binding_sets_the_run_age_bound_on_every_check(
    tmp_path: Path,
) -> None:
    settings = load_floor_fill_alarm_settings(_section(tmp_path))
    request = settings.check_request(datetime.now(UTC))
    assert request.max_run_age_minutes == MAX_RUN_AGE_MINUTES


def test_floor_fill_alarm_binding_a_section_without_the_run_age_bound_is_refused(
    tmp_path: Path,
) -> None:
    section = _section(tmp_path)
    del section["max_run_age_minutes"]
    with pytest.raises(FloorFillAlarmConfigError) as refused:
        load_floor_fill_alarm_settings(section)
    assert "floor_fill_alarm.max_run_age_minutes" in refused.value.keys


def test_floor_fill_alarm_binding_an_append_command_without_the_row_is_refused(
    tmp_path: Path,
) -> None:
    section = _section(tmp_path)
    section["ledger_append_command"] = [sys.executable, "-c", "pass"]
    with pytest.raises(FloorFillAlarmConfigError):
        load_floor_fill_alarm_settings(section)


def test_floor_fill_alarm_binding_logs_each_check_and_each_failed_check(
    tmp_path: Path,
) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir()
    _out, _code, store = live_tick(tick_dir, free=2, age_seconds=5)
    section = _section(tick_dir)
    settings = load_floor_fill_alarm_settings(section)
    tick_at = datetime.now(UTC)
    run_alarm_check(
        settings,
        load_payload=store_payload_loader(store),
        now=tick_at + timedelta(minutes=1),
        pool_clock=lambda: tick_at,
    )

    def broken(_key: str) -> str | None:
        raise OSError("store gone")

    with pytest.raises(AlarmSourceError):
        run_alarm_check(
            settings,
            load_payload=broken,
            now=tick_at + timedelta(minutes=2),
            pool_clock=lambda: tick_at,
        )
    lines = [
        json.loads(line)
        for line in Path(section["check_log_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [line["outcome"] for line in lines] == ["checked", "failed"]
    assert lines[0]["runs_read"] == 1
    assert lines[0]["lanes_read"] == 2
    assert "store gone" in lines[1]["error"]
