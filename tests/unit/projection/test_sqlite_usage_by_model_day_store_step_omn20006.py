# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The usage-by-model-day columns and cursor triggers are a one-time store step.

A local store written before the usage-by-model-day exposure lacks three
aggregate columns, the calls table's ``usage_source`` and the two cursor
triggers. ``SqliteDatabaseAdapter`` adds them on connect, the way it moves
``llm_call_metrics.usage_source`` onto the shared vocabulary: once per store,
committed together with its row in the store-steps table, and skipped by every
later connection, reads included.

Each test names the failure it exists to catch:

* a read-only store written before the change raises on every read, of every
  table, instead of being read as it is (the reconcile's write is not optional
  for a store this process cannot write);
* the same, for a store whose folder is read-only (SQLite cannot create its
  rollback journal; the extended code is SQLITE_READONLY_DIRECTORY);
* the step adds the columns but is not recorded, or is recorded but a later
  read still takes the write lock;
* the columns are added but the step record fails, so a half-done step is
  committed and never retried as a whole;
* a busy writable store is passed off as read-only and read without the
  columns;
* two first opens of one store race and the second adds a column twice.

Every store is a SQLite file under pytest's ``tmp_path``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection import sqlite_database
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

# The adapter's bookkeeping, named here as literals rather than imported so
# these tests also run against code that predates the usage step.
STEPS_TABLE = "omnimarket_sqlite_store_steps"
USAGE_STEP = "omn20006_usage_by_model_day_measured_cost"
ADDED_COLUMNS = {
    "usage_by_model_day_calls": {"usage_source"},
    "usage_by_model_day": {
        "measured_cost_usd",
        "unmeasured_call_count",
        "projection_cursor",
    },
}
CURSOR_TRIGGERS = {
    "usage_by_model_day_cursor_on_insert",
    "usage_by_model_day_cursor_on_recount",
}


def _store_written_before_the_change(path: Path) -> None:
    """A store as the release before this change leaves it: every other step
    applied (the usage-source vocabulary step recorded), the usage tables in
    their first shape, no cursor triggers, no usage step recorded."""
    with closing(SqliteDatabaseAdapter(path)._connect()) as conn:
        conn.execute(
            "INSERT INTO llm_call_metrics (model_id, input_hash, usage_source) "
            "VALUES ('m', 'h-1', 'measured')"
        )
        for trigger in CURSOR_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS usage_by_model_day_projection_cursor_seq")
        conn.execute("DROP TABLE usage_by_model_day_calls")
        conn.execute("DROP TABLE usage_by_model_day")
        conn.execute(
            "CREATE TABLE usage_by_model_day_calls (call_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
            "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
            "cost_usd REAL NOT NULL, occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE usage_by_model_day (tenant_id TEXT NOT NULL, "
            "usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
            "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
            "cost_usd REAL NOT NULL, call_count INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, usage_day, model_id))"
        )
        conn.execute(f"DELETE FROM {STEPS_TABLE} WHERE step = ?", (USAGE_STEP,))
        conn.commit()


def _missing(path: Path) -> dict[str, set[str]]:
    """The added columns and triggers the file lacks, read past the adapter."""
    with closing(sqlite3.connect(path)) as conn:
        missing = {
            table: columns
            - {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            for table, columns in ADDED_COLUMNS.items()
        }
        missing["triggers"] = CURSOR_TRIGGERS - {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    return {k: v for k, v in missing.items() if v}


def _steps(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {str(r[0]) for r in conn.execute(f"SELECT step FROM {STEPS_TABLE}")}


@contextmanager
def _read_only(path: Path) -> Iterator[None]:
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        if os.access(path, os.W_OK):
            pytest.fail(
                f"{path} is still writable with mode 0o444 (running as root?); "
                "this test cannot prove a read-only store is read"
            )
        yield
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)


@contextmanager
def _held_by_another_writer(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Another connection holds a write transaction; the adapter does not wait,
    so a write lock taken on connect shows as an error, not a 5 s stall."""
    connect = sqlite3.connect

    def connect_without_waiting(
        database: Path, *args: Any, **kwargs: Any
    ) -> sqlite3.Connection:
        conn: sqlite3.Connection = connect(database, *args, **{**kwargs, "timeout": 0})
        return conn

    writer = connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    # The adapter calls sqlite3.connect through the module it imports.
    monkeypatch.setattr(sqlite3, "connect", connect_without_waiting)
    try:
        yield
    finally:
        monkeypatch.undo()
        writer.rollback()
        writer.close()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == sqlite_database.__name__ and record.levelno == logging.WARNING
    ]


def test_a_read_only_store_written_before_the_change_is_read_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with _read_only(path):
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert [row["input_hash"] for row in rows] == ["h-1"]
    assert _missing(path) == {**ADDED_COLUMNS, "triggers": CURSOR_TRIGGERS}
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert str(path) in warnings[0].getMessage()


def test_a_store_in_a_read_only_folder_written_before_the_change_is_read_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    path.parent.chmod(0o555)
    try:
        if os.access(path.parent, os.W_OK):
            pytest.fail(
                f"{path.parent} is still writable with mode 0o555 (running as "
                "root?); this test cannot prove a store in a read-only folder is read"
            )
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")
    finally:
        path.parent.chmod(0o755)

    assert [row["input_hash"] for row in rows] == ["h-1"]
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert str(path) in warnings[0].getMessage()


def test_the_first_open_applies_and_records_the_step_and_a_later_read_takes_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)

    SqliteDatabaseAdapter(path).query("usage_by_model_day")

    assert _missing(path) == {}
    assert USAGE_STEP in _steps(path)
    with _held_by_another_writer(path, monkeypatch):
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")
    assert [row["input_hash"] for row in rows] == ["h-1"]


def test_a_failed_step_record_leaves_the_columns_unadded(tmp_path: Path) -> None:
    # The columns, the triggers and the step's record are one transaction: if
    # the record cannot be written, nothing else may stay behind either, so the
    # next connect retries the whole step rather than trusting a half-done one.
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            f"CREATE TRIGGER refuse_usage_step BEFORE INSERT ON {STEPS_TABLE} "
            f"WHEN NEW.step = '{USAGE_STEP}' "
            "BEGIN SELECT RAISE(ABORT, 'step record refused'); END"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="step record refused"):
        SqliteDatabaseAdapter(path).query("usage_by_model_day")

    assert _missing(path) == {**ADDED_COLUMNS, "triggers": CURSOR_TRIGGERS}
    assert USAGE_STEP not in _steps(path)


def test_a_locked_store_written_before_the_change_raises_rather_than_reading(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with (
        _held_by_another_writer(path, monkeypatch),
        pytest.raises(sqlite3.OperationalError, match="locked"),
    ):
        SqliteDatabaseAdapter(path).query("usage_by_model_day")

    assert _warnings(caplog) == []
    assert USAGE_STEP not in _steps(path)


def test_two_first_opens_together_add_each_column_once(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    _store_written_before_the_change(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def open_store() -> None:
        barrier.wait()
        try:
            SqliteDatabaseAdapter(path).query("usage_by_model_day")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _missing(path) == {}
    assert USAGE_STEP in _steps(path)
