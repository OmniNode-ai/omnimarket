# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SQLite store's per-write column step adds each column exactly once.

``SqliteDatabaseAdapter`` adds a column when a written row carries one the
table lacks. That step read ``PRAGMA table_info`` with no lock and then ran one
autocommit ``ALTER TABLE ... ADD COLUMN`` per missing column, so two writers
reaching a fresh store together could both see a column missing, and the second
ALTER failed with ``duplicate column name``: that writer raised and its row was
lost. A real delegation row carries columns an open does not create, so the
first writes of every fresh store take this path.

Each test names the failure it exists to catch:

* writers arriving together on a fresh store, as threads or as separate
  processes, add the same column twice, so a writer dies on a duplicate column
  name or a locked database and its row is lost;
* the same race on a table other than ``delegation_events``;
* a write whose columns all exist still takes the write lock for the schema
  step, so steady-state writes queue behind it;
* a failed column addition leaves part of the schema behind, or keeps the
  write lock held.

Every store is a SQLite file under pytest's ``tmp_path``.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_DELEGATION_EVENTS = "delegation_events"
_CLAIMS = "delegate_skill_command_claims"
_WORKERS = 8
_STORES = 12
_PROCESS_STORES = 8


class _NullPublisher:
    """No broker here; the snapshot republish is not what this module proves."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


def _correlation_id(store: int, worker: int) -> str:
    return f"19976000-0000-4000-8000-{store:06d}{worker:06d}"


def _write_delegation(adapter: SqliteDatabaseAdapter, correlation_id: str) -> None:
    # One real projection write: the handler builds the row, so the columns
    # the race is about are the ones production writes.
    HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
        {
            "status": "completed",
            "correlation_id": correlation_id,
            "task_type": "research",
            "tenant_id": "omninode",
            "metrics": {"cost_usd": 0.0},
            "timestamp": "2026-10-04T12:00:00+00:00",
            "_db": adapter,
        }
    )


def _write_claim(adapter: SqliteDatabaseAdapter, worker: int) -> None:
    adapter.upsert(
        _CLAIMS,
        "delivery_id",
        {
            "delivery_id": f"d-{worker}",
            "claimed_at": "2026-10-04T12:00:00+00:00",
            # As many new columns as a delegation row brings to a fresh store.
            **{f"note_{n}": worker for n in range(10)},
        },
    )


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(count)
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _run_together(workers: int, work: Callable[[int], None]) -> list[str]:
    barrier = threading.Barrier(workers)
    errors: list[str] = []
    errors_lock = threading.Lock()

    def run(worker: int) -> None:
        barrier.wait()
        try:
            work(worker)
        except BaseException as exc:
            with errors_lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_concurrent_first_writes_of_one_store_all_land(tmp_path: Path) -> None:
    failures: dict[int, list[str]] = {}
    rows: dict[int, int] = {}
    for store in range(_STORES):
        db_path = tmp_path / f"delegation-{store}.sqlite"

        def write(worker: int, store: int = store, db_path: Path = db_path) -> None:
            _write_delegation(
                SqliteDatabaseAdapter(db_path), _correlation_id(store, worker)
            )

        errors = _run_together(_WORKERS, write)
        if errors:
            failures[store] = errors
        rows[store] = _count(db_path, _DELEGATION_EVENTS)
    assert failures == {}
    assert rows == dict.fromkeys(range(_STORES), _WORKERS)


def test_concurrent_first_writes_to_another_table_all_land(tmp_path: Path) -> None:
    failures: dict[int, list[str]] = {}
    rows: dict[int, int] = {}
    for store in range(_STORES):
        db_path = tmp_path / f"claims-{store}.sqlite"

        def claim(worker: int, db_path: Path = db_path) -> None:
            _write_claim(SqliteDatabaseAdapter(db_path), worker)

        errors = _run_together(_WORKERS, claim)
        if errors:
            failures[store] = errors
        rows[store] = _count(db_path, _CLAIMS)
    assert failures == {}
    assert rows == dict.fromkeys(range(_STORES), _WORKERS)


def _write_in_child(
    db_paths: list[str], worker: int, barrier: Any, results: Any
) -> None:
    errors: list[str] = []
    for store, db_path in enumerate(db_paths):
        barrier.wait(timeout=60)
        try:
            _write_delegation(
                SqliteDatabaseAdapter(Path(db_path)), _correlation_id(store, worker)
            )
        except BaseException as exc:
            errors.append(f"store {store}: {exc!r}")
    results.put((worker, errors))


def test_concurrent_first_writes_from_separate_processes_all_land(
    tmp_path: Path,
) -> None:
    # A lock held only inside one process would pass the thread test and fail
    # here: the store is one file shared by every process that writes it.
    db_paths = [
        str(tmp_path / f"delegation-{store}.sqlite") for store in range(_PROCESS_STORES)
    ]
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_WORKERS)
    results: Any = ctx.Queue()
    procs = [
        ctx.Process(target=_write_in_child, args=(db_paths, w, barrier, results))
        for w in range(_WORKERS)
    ]
    for proc in procs:
        proc.start()
    reported = dict(results.get(timeout=180) for _ in procs)
    for proc in procs:
        proc.join(timeout=30)
    assert sorted(reported) == list(range(_WORKERS)), "a child never reported"
    assert {w: errs for w, errs in reported.items() if errs} == {}
    assert [_count(Path(p), _DELEGATION_EVENTS) for p in db_paths] == [
        _WORKERS
    ] * _PROCESS_STORES


class _TracingAdapter(SqliteDatabaseAdapter):
    """Records every statement its connections run after they are set up."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.statements: list[str] = []

    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        conn.set_trace_callback(self.statements.append)
        return conn


def _schema_locks(statements: list[str]) -> list[str]:
    return [s for s in statements if s.upper().startswith("BEGIN IMMEDIATE")]


def test_only_a_write_that_adds_a_column_takes_the_schema_lock(
    tmp_path: Path,
) -> None:
    adapter = _TracingAdapter(tmp_path / "delegation.sqlite")
    # A fresh store: the first write adds columns, under the write lock. This
    # half also proves the trace sees the lock statement at all.
    _write_delegation(adapter, _correlation_id(0, 0))
    assert len(_schema_locks(adapter.statements)) == 1, adapter.statements
    adapter.statements.clear()
    # Every column the next row carries now exists: no lock for the schema step.
    _write_delegation(adapter, _correlation_id(0, 1))
    assert adapter.statements, "the trace saw no statement at all"
    assert _schema_locks(adapter.statements) == []


class _FailingAlterConnection:
    """A connection whose ALTER adding ``fail_column`` raises."""

    def __init__(self, conn: sqlite3.Connection, fail_column: str) -> None:
        self._conn = conn
        self._fail_column = fail_column

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.startswith("ALTER TABLE") and sql.endswith(
            f"ADD COLUMN {self._fail_column}"
        ):
            raise sqlite3.OperationalError(f"refused: {sql}")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_a_failed_column_addition_leaves_no_partial_schema_and_no_lock(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "claims.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    conn = adapter._connect()
    try:
        failing = cast(sqlite3.Connection, _FailingAlterConnection(conn, "second"))
        with pytest.raises(sqlite3.OperationalError, match="refused"):
            adapter._ensure_columns(
                failing,
                _CLAIMS,
                {"delivery_id": "d-0", "first": 1, "second": 2},
            )
        # Rolled back on the connection itself, not only when it is closed.
        assert conn.in_transaction is False
        other = sqlite3.connect(db_path, timeout=0)
        try:
            assert {"first", "second"}.isdisjoint(_columns(other, _CLAIMS))
            # The write lock was released: another writer takes it at once.
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()
    finally:
        conn.close()


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows


class _RaceAfterFirstLook:
    """Another writer adds ``columns`` right after this connection's first look.

    That is the interleaving the race needs, made deterministic: the schema
    step has read the table and found a column missing, and before it alters
    the table a second writer adds that column.
    """

    def __init__(
        self, conn: sqlite3.Connection, db_path: Path, table: str, columns: list[str]
    ) -> None:
        self._conn = conn
        self._db_path = db_path
        self._table = table
        self._columns = columns
        self.looks = 0

    def execute(self, sql: str, *args: Any) -> Any:
        if sql != f"PRAGMA table_info({self._table})":
            return self._conn.execute(sql, *args)
        rows = self._conn.execute(sql, *args).fetchall()
        self.looks += 1
        if self.looks == 1:
            other = sqlite3.connect(self._db_path)
            try:
                for column in self._columns:
                    other.execute(f"ALTER TABLE {self._table} ADD COLUMN {column}")
                other.commit()
            finally:
                other.close()
        return _Rows(rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_a_column_added_between_the_check_and_the_lock_is_not_added_again(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "claims.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    conn = adapter._connect()
    try:
        raced = _RaceAfterFirstLook(conn, db_path, _CLAIMS, ["first"])
        adapter._ensure_columns(
            cast(sqlite3.Connection, raced),
            _CLAIMS,
            {"delivery_id": "d-0", "first": 1, "second": 2},
        )
        assert raced.looks == 2, "the schema was not read again under the lock"
        assert {"first", "second"} <= _columns(conn, _CLAIMS)
    finally:
        conn.close()
