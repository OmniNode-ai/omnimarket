# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watchdog's local durable store: carried state and the delivery outbox.

Deciding is one transaction: the new outbox rows and the state the compute
asked to carry commit together or not at all. Every later change to a row is
its own transaction, so a restart finds each row exactly as the last
committed step left it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from omnimarket.models.liveness.model_automation_liveness import EnumAlarmSeverity
from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    EnumOutboxStatus,
    ModelOutboxEntry,
)

_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS watchdog_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT NOT NULL
)""",
    """
CREATE TABLE IF NOT EXISTS outbox (
    episode_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    delivered_at TEXT,
    PRIMARY KEY (episode_id, severity)
)""",
)


class OutboxStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    def load_state(self) -> dict[str, object] | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT state_json FROM watchdog_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        loaded: dict[str, object] = json.loads(row[0])
        return loaded

    def commit_decision(
        self,
        entries: Sequence[ModelOutboxEntry],
        carried_state: dict[str, object] | None,
    ) -> None:
        """Queue the new rows and carry the state, in one transaction."""
        with self._transaction() as db:
            for entry in entries:
                db.execute(
                    "INSERT OR IGNORE INTO outbox "
                    "(episode_id, severity, entry_json, status, opened_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(entry.episode_id),
                        entry.severity.value,
                        entry.model_dump_json(),
                        entry.status.value,
                        entry.opened_at.isoformat(),
                    ),
                )
                if entry.severity is EnumAlarmSeverity.HIGH:
                    self._supersede_low(db, entry.episode_id)
            if carried_state is not None:
                db.execute(
                    "INSERT INTO watchdog_state (id, state_json) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
                    (json.dumps(carried_state, sort_keys=True),),
                )

    def _supersede_low(self, db: sqlite3.Connection, episode_id: UUID) -> None:
        low = self._read(db, episode_id, EnumAlarmSeverity.LOW)
        if low is not None and low.status is EnumOutboxStatus.PENDING:
            self._write(
                db, low.model_copy(update={"status": EnumOutboxStatus.SUPERSEDED})
            )

    def pending(self) -> list[ModelOutboxEntry]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT entry_json FROM outbox WHERE status = ? ORDER BY opened_at, episode_id",
                (EnumOutboxStatus.PENDING.value,),
            ).fetchall()
        return [ModelOutboxEntry.model_validate_json(row[0]) for row in rows]

    def delivered_since(self, since: datetime) -> int:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT delivered_at FROM outbox WHERE status = ? AND delivered_at IS NOT NULL",
                (EnumOutboxStatus.DELIVERED.value,),
            ).fetchall()
        return sum(1 for (stamp,) in rows if datetime.fromisoformat(stamp) > since)

    def mark_attempt(self, entry: ModelOutboxEntry) -> ModelOutboxEntry:
        """Record that a send is about to start, before it can be accepted."""
        updated = entry.model_copy(update={"attempts": entry.attempts + 1})
        with self._transaction() as db:
            self._write(db, updated)
        return updated

    def record_attempt_failure(
        self, entry: ModelOutboxEntry, failure: str
    ) -> ModelOutboxEntry:
        updated = entry.model_copy(update={"last_failure": failure})
        with self._transaction() as db:
            self._write(db, updated)
        return updated

    def record_delivery(
        self, entry: ModelOutboxEntry, message_ref: str, now: datetime
    ) -> ModelOutboxEntry:
        updated = entry.model_copy(
            update={
                "status": EnumOutboxStatus.DELIVERED,
                "message_ref": message_ref,
                "delivered_at": now,
                "last_failure": None,
            }
        )
        with self._transaction() as db:
            self._write(db, updated)
        return updated

    def record_escalation(self, entry: ModelOutboxEntry, ref: str) -> ModelOutboxEntry:
        updated = entry.model_copy(update={"escalation_ref": ref})
        with self._transaction() as db:
            self._write(db, updated)
        return updated

    @staticmethod
    def _write(db: sqlite3.Connection, entry: ModelOutboxEntry) -> None:
        db.execute(
            "UPDATE outbox SET entry_json = ?, status = ?, delivered_at = ? "
            "WHERE episode_id = ? AND severity = ?",
            (
                entry.model_dump_json(),
                entry.status.value,
                entry.delivered_at.isoformat() if entry.delivered_at else None,
                str(entry.episode_id),
                entry.severity.value,
            ),
        )

    @staticmethod
    def _read(
        db: sqlite3.Connection, episode_id: UUID, severity: EnumAlarmSeverity
    ) -> ModelOutboxEntry | None:
        row = db.execute(
            "SELECT entry_json FROM outbox WHERE episode_id = ? AND severity = ?",
            (str(episode_id), severity.value),
        ).fetchone()
        return None if row is None else ModelOutboxEntry.model_validate_json(row[0])


__all__ = ["OutboxStore"]
