# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dead-man's local record of the ledger rows it has written (OMN-20807).

One row per (episode, phase), so a duplicate delivery of the same episode event
finds its key here and writes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_automation_liveness_deadman_compute.models.model_automation_liveness_deadman import (
    EnumDeadmanPhase,
    ModelDeadmanEpisodeKey,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deadman_recorded (
    episode_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    ledger_line TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (episode_id, phase)
)"""


class RecordedStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(_SCHEMA)

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

    def recorded_keys(self) -> tuple[ModelDeadmanEpisodeKey, ...]:
        with self._transaction() as db:
            found = db.execute(
                "SELECT episode_id, phase FROM deadman_recorded"
            ).fetchall()
        return tuple(
            ModelDeadmanEpisodeKey(episode_id=e, phase=EnumDeadmanPhase(p))
            for e, p in found
        )

    def mark_recorded(
        self, key: ModelDeadmanEpisodeKey, ledger_line: str, at: datetime
    ) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO deadman_recorded "
                "(episode_id, phase, ledger_line, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    str(key.episode_id),
                    key.phase.value,
                    ledger_line,
                    at.astimezone(UTC).isoformat(),
                ),
            )

    def recorded_count(self) -> int:
        with self._transaction() as db:
            (count,) = db.execute("SELECT COUNT(*) FROM deadman_recorded").fetchone()
        return int(count)

    def last_recorded_at(self) -> datetime | None:
        with self._transaction() as db:
            row = db.execute("SELECT MAX(recorded_at) FROM deadman_recorded").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None
