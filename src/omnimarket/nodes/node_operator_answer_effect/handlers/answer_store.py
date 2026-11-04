# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable answer state and ledger outbox, with locks surviving process replacement."""

import fcntl
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from omnimarket.models.operator_answer import ModelAnswerRow, ModelAnswerState


class SqliteAnswerStore:
    def __init__(self, path: Path | None = None) -> None:
        with self._storage():
            self._path = path
            self._mutex = threading.RLock()
            self._db = sqlite3.connect(
                str(path) if path else ":memory:", check_same_thread=False
            )
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(
                "CREATE TABLE IF NOT EXISTS answer_state (key TEXT PRIMARY KEY, body TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS answer_rows (id TEXT PRIMARY KEY, body TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0);"
            )
            self._db.commit()

    @contextmanager
    def _storage(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as exc:
            raise OSError("answer store unavailable") from exc

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        # A database-wide lock serializes writers across processes. Commits within it
        # remain durable when a process dies during a channel call.
        with self._mutex:
            if self._path is None:
                yield
            else:
                with self._path.with_suffix(self._path.suffix + ".lock").open(
                    "a"
                ) as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def get(self, key: str) -> ModelAnswerState | None:
        with self._storage():
            row = self._db.execute(
                "SELECT body FROM answer_state WHERE key=?", (key,)
            ).fetchone()
            return ModelAnswerState.model_validate_json(row[0]) if row else None

    def states(self) -> tuple[ModelAnswerState, ...]:
        with self._storage():
            return tuple(
                ModelAnswerState.model_validate_json(row[0])
                for row in self._db.execute(
                    "SELECT body FROM answer_state ORDER BY key"
                )
            )

    def save(self, state: ModelAnswerState, row: ModelAnswerRow | None = None) -> None:
        with self._storage(), self._db:
            self._db.execute(
                "INSERT INTO answer_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                (state.message.answer_key, state.model_dump_json()),
            )
            if row is not None:
                self._db.execute(
                    "INSERT OR IGNORE INTO answer_rows(id, body) VALUES (?, ?)",
                    (row.row_id, row.model_dump_json()),
                )

    def pending_rows(self) -> tuple[ModelAnswerRow, ...]:
        with self._storage():
            return tuple(
                ModelAnswerRow.model_validate_json(row[0])
                for row in self._db.execute(
                    "SELECT body FROM answer_rows WHERE exported=0 ORDER BY rowid"
                )
            )

    def exported(self, row_id: str) -> None:
        with self._storage(), self._db:
            self._db.execute("UPDATE answer_rows SET exported=1 WHERE id=?", (row_id,))

    def rows(self) -> tuple[ModelAnswerRow, ...]:
        with self._storage():
            return tuple(
                ModelAnswerRow.model_validate_json(row[0])
                for row in self._db.execute(
                    "SELECT body FROM answer_rows ORDER BY rowid"
                )
            )

    def close(self) -> None:
        with self._storage():
            self._db.close()
