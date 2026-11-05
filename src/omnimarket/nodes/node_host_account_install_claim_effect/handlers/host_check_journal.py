# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick, both cursors and its outbox commit together before publication."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from omnimarket.models.host_checks import ModelHostCheckEvent, ModelHostCheckTickResult


class MemoryHostCheckJournal:
    def __init__(self) -> None:
        self.results: dict[str, ModelHostCheckTickResult] = {}
        self.events: dict[str, ModelHostCheckEvent] = {}
        self.sent: set[str] = set()
        self._lock = RLock()

    def result(self, tick_id: str) -> ModelHostCheckTickResult | None:
        with self._lock:
            return self.results.get(tick_id)

    def record(
        self, result: ModelHostCheckTickResult, events: tuple[ModelHostCheckEvent, ...]
    ) -> ModelHostCheckTickResult:
        with self._lock:
            if result.tick_id in self.results:
                return self.results[result.tick_id]
            added = 0
            for event in events:
                if event.key not in self.events:
                    self.events[event.key] = event
                    added += event.key.startswith("exception:")
            result = result.model_copy(update={"exceptions_added": added})
            self.results[result.tick_id] = result
            return result

    def pending(self) -> tuple[ModelHostCheckEvent, ...]:
        with self._lock:
            return tuple(v for k, v in self.events.items() if k not in self.sent)

    def delivered(self, key: str) -> None:
        with self._lock:
            self.sent.add(key)


class SqliteHostCheckJournal:
    def __init__(self, path: str, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS host_check_ticks (tick_id TEXT PRIMARY KEY, result TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS host_check_cursors (tick_id TEXT, check_id TEXT, at TEXT, row TEXT NOT NULL, PRIMARY KEY(tick_id, check_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS host_check_outbox (key TEXT PRIMARY KEY, event TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def result(self, tick_id: str) -> ModelHostCheckTickResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT result FROM host_check_ticks WHERE tick_id=?", (tick_id,)
            ).fetchone()
        return ModelHostCheckTickResult.model_validate_json(row[0]) if row else None

    def record(
        self, result: ModelHostCheckTickResult, events: tuple[ModelHostCheckEvent, ...]
    ) -> ModelHostCheckTickResult:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT result FROM host_check_ticks WHERE tick_id=?", (result.tick_id,)
            ).fetchone()
            if prior:
                return ModelHostCheckTickResult.model_validate_json(prior[0])
            added = 0
            for event in events:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO host_check_outbox(key,event) VALUES (?,?)",
                    (event.key, event.model_dump_json()),
                ).rowcount
                added += inserted if event.key.startswith("exception:") else 0
            result = result.model_copy(update={"exceptions_added": added})
            db.execute(
                "INSERT INTO host_check_ticks VALUES (?,?)",
                (result.tick_id, result.model_dump_json()),
            )
            for cursor in result.cursors:
                db.execute(
                    "INSERT INTO host_check_cursors VALUES (?,?,?,?)",
                    (
                        cursor.tick_id,
                        cursor.check,
                        cursor.at.isoformat(),
                        cursor.model_dump_json(),
                    ),
                )
            return result

    def pending(self) -> tuple[ModelHostCheckEvent, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT event FROM host_check_outbox WHERE delivered=0 ORDER BY rowid"
            ).fetchall()
        return tuple(ModelHostCheckEvent.model_validate_json(row[0]) for row in rows)

    def delivered(self, key: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE host_check_outbox SET delivered=1 WHERE key=?", (key,))
