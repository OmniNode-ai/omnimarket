# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable lab test binding; this is not the deployed Part A claim store."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from omnimarket.models.model_rule_premise import ModelBatchSlot, ModelBatchState
from omnimarket.protocols.protocol_rule_premise import ProtocolBatchTransaction


class _SqliteTransaction:
    def __init__(self, connection: sqlite3.Connection, batch: str) -> None:
        self.connection, self.batch = connection, batch

    def read(self) -> ModelBatchState | None:
        row = self.connection.execute(
            "SELECT state FROM batches WHERE batch = ?", (self.batch,)
        ).fetchone()
        return ModelBatchState.model_validate_json(row[0]) if row else None

    def insert_slot(self, slot: ModelBatchSlot) -> bool:
        cursor = self.connection.execute(
            "INSERT INTO slots (slot_key, batch, brief_id) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (slot.key, self.batch, slot.brief_id),
        )
        return cursor.rowcount == 1

    def write(self, state: ModelBatchState) -> None:
        self.connection.execute(
            "INSERT INTO batches (batch, state) VALUES (?, ?) ON CONFLICT(batch) DO UPDATE SET state = excluded.state",
            (self.batch, state.model_dump_json()),
        )
        if state.escalation:
            self.connection.execute(
                "INSERT INTO escalations (escalation_key, batch) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (state.escalation, self.batch),
            )


class SqliteBatchClaimStore:
    def __init__(self, path: Path, timeout_s: float) -> None:
        self.path, self.timeout_s = path, timeout_s
        with sqlite3.connect(path, timeout=timeout_s) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS batches (batch TEXT PRIMARY KEY, state TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS slots (slot_key TEXT PRIMARY KEY, batch TEXT NOT NULL, brief_id TEXT NOT NULL, UNIQUE(batch, brief_id));"
                "CREATE TABLE IF NOT EXISTS escalations (escalation_key TEXT PRIMARY KEY, batch TEXT NOT NULL);"
            )

    @contextmanager
    def transaction(self, batch: str) -> Iterator[ProtocolBatchTransaction]:
        try:
            with sqlite3.connect(self.path, timeout=self.timeout_s) as connection:
                connection.execute("BEGIN IMMEDIATE")
                yield _SqliteTransaction(connection, batch)
        except sqlite3.Error as exc:
            raise OSError(f"batch claim transaction failed: {exc}") from exc
