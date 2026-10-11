# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The automation-liveness writer persists its fold and a restart loses nothing (OMN-20802).

Every store is a SQLite file under pytest's ``tmp_path``, the file the local
profile's projection read node serves. Every assertion that a row was written
reads the file with a separate connection, so a writer that only advanced
offsets would fail here.

Each test names the failure it exists to catch:

* the writer reports ``rows_upserted`` while the file holds no row;
* a writer built on the same file after a stop starts from nothing, so the run
  history behind ``failures_in_window`` and the idle streak is gone;
* a replayed event after the restart writes a row again;
* a stream split across two writers ends in different rows than one writer's;
* a payload of the wrong event for its topic is accepted by the union.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent as Kind,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_automation_liveness_writer import (
    AutomationLivenessProjectionWriter,
)
from omnimarket.nodes.node_projection_automation_liveness.ports.liveness_store import (
    SqliteLivenessStore,
)
from tests.helpers.automation_liveness_stream import (
    ACTIVE_HOST,
    ACTIVE_PROCESS,
    SILENT_HOST,
    SILENT_PROCESS,
    STREAM_ORDER,
    fixture_payload,
    fixture_topic,
)

pytestmark = pytest.mark.unit


def _writer(db_path: Path) -> AutomationLivenessProjectionWriter:
    return AutomationLivenessProjectionWriter(store=SqliteLivenessStore(db_path))


def _handle(
    writer: AutomationLivenessProjectionWriter, kind: Kind, offset: int
) -> dict[str, Any]:
    return writer.handle(
        {
            **fixture_payload(kind),
            "_topic": fixture_topic(kind),
            "_partition": 0,
            "_offset": offset,
        }
    )


def _dump(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every stored row, without the write-time stamp, read on its own connection."""
    conn = sqlite3.connect(db_path)
    try:
        out: dict[str, list[tuple[object, ...]]] = {}
        for table, order in (
            ("automation_liveness_state", "process_key"),
            ("automation_run_history", "run_key"),
            ("automation_alarm_episodes", "episode_id"),
        ):
            columns = [
                r[1]
                for r in conn.execute(f"PRAGMA table_info({table})")
                if r[1] != "projected_at"
            ]
            out[table] = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
            ).fetchall()
        return out
    finally:
        conn.close()


def _write_stream(writer: AutomationLivenessProjectionWriter) -> list[int]:
    return [
        _handle(writer, kind, offset)["rows_upserted"]
        for offset, kind in enumerate(STREAM_ORDER)
    ]


def test_automation_liveness_writer_restart_persists_the_rows_it_reports(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "liveness.sqlite"
    counts = _write_stream(_writer(db_path))
    assert all(count >= 1 for count in counts), counts
    rows = _dump(db_path)
    assert {r[0] for r in rows["automation_liveness_state"]} == {
        f"{ACTIVE_PROCESS}@{ACTIVE_HOST}",
        f"{SILENT_PROCESS}@{SILENT_HOST}",
        "host-c/watchdog-primary@host-c",
    }
    assert len(rows["automation_run_history"]) == 1
    assert len(rows["automation_alarm_episodes"]) == 1


def test_automation_liveness_writer_restart_loses_nothing_and_replay_writes_nothing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "liveness.sqlite"
    _write_stream(_writer(db_path))
    before = _dump(db_path)

    restarted = _writer(db_path)
    replay = _write_stream(restarted)
    assert replay == [0] * len(STREAM_ORDER), "a replayed event wrote a row"
    assert _dump(db_path) == before


def test_automation_liveness_writer_restart_folds_new_events_over_the_stored_history(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "liveness.sqlite"
    _write_stream(_writer(db_path))

    failed = {
        **fixture_payload(Kind.RUN_OBSERVED),
        "run_id": "example-interval-job:2026-10-09T02:15:00Z",
        "started_at": "2026-10-09T02:15:00Z",
        "finished_at": "2026-10-09T02:15:40Z",
        "observed_at": "2026-10-09T02:16:00Z",
        "outcome": "failed",
        "exit_code": 1,
    }
    result = _writer(db_path).handle(
        {
            **failed,
            "_topic": fixture_topic(Kind.RUN_OBSERVED),
            "_partition": 0,
            "_offset": 100,
        }
    )
    assert result["rows_upserted"] == 2, "one run row and the process row"

    conn = sqlite3.connect(db_path)
    try:
        failures, idle, last_outcome = conn.execute(
            "SELECT failures_in_window, consecutive_idle_with_demand, last_outcome "
            "FROM automation_liveness_state WHERE process_key = ?",
            (f"{ACTIVE_PROCESS}@{ACTIVE_HOST}",),
        ).fetchone()
        runs = conn.execute("SELECT count(*) FROM automation_run_history").fetchone()[0]
    finally:
        conn.close()
    assert (failures, idle, last_outcome) == (1, 0, "failed")
    assert runs == 2, "the run stored before the restart is still there"


def test_automation_liveness_writer_restart_split_stream_equals_one_writer(
    tmp_path: Path,
) -> None:
    whole = tmp_path / "whole.sqlite"
    split = tmp_path / "split.sqlite"
    _write_stream(_writer(whole))

    first, rest = STREAM_ORDER[:3], STREAM_ORDER[3:]
    one = _writer(split)
    for offset, kind in enumerate(first):
        _handle(one, kind, offset)
    two = _writer(split)
    for offset, kind in enumerate(rest, start=len(first)):
        _handle(two, kind, offset)
    assert _dump(split) == _dump(whole)


def test_automation_liveness_writer_restart_refuses_a_payload_of_another_event(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path / "liveness.sqlite")
    with pytest.raises(ValidationError):
        writer.handle(
            {
                **fixture_payload(Kind.HEARTBEAT),
                "_topic": fixture_topic(Kind.RUN_OBSERVED),
                "_partition": 0,
                "_offset": 0,
            }
        )
    assert _dump(tmp_path / "liveness.sqlite")["automation_liveness_state"] == []
