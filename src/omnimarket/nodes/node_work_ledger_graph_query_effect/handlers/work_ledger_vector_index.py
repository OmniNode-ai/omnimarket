# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persist the local ledger vector index and filter candidate rows."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.work_ledger_graph.model_work_ledger_similarity import (
    ModelLedgerSimilarityRequest,
    ModelLedgerSimilarityRow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_model (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    name TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK (dimension > 0)
);
CREATE TABLE IF NOT EXISTS ledger_vector (
    ledger_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    row_ts TEXT NOT NULL,
    row_type TEXT NOT NULL,
    ticket TEXT NOT NULL,
    lane TEXT,
    kind TEXT,
    pr TEXT,
    head TEXT,
    outcome TEXT,
    text TEXT NOT NULL,
    question TEXT,
    vector BLOB,
    question_vector BLOB,
    PRIMARY KEY (ledger_id, row_id)
);
CREATE INDEX IF NOT EXISTS ledger_vector_kind_stamp
    ON ledger_vector (ledger_id, row_type, row_ts, row_id);
CREATE INDEX IF NOT EXISTS ledger_vector_lane_kind_stamp
    ON ledger_vector (ledger_id, lane, row_type, row_ts, row_id);
"""
_ROW_COLUMNS = (
    "row_id, row_ts, row_type, ticket, lane, kind, pr, head, outcome, text, question"
)
_TAG_COLUMNS = ("lane", "kind", "pr", "head", "outcome")
_UPSERT = """
INSERT INTO ledger_vector (
    ledger_id, row_id, row_ts, row_type, ticket, lane, kind, pr, head, outcome, text, question, vector, question_vector
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ledger_id, row_id) DO UPDATE SET
    row_ts=excluded.row_ts, row_type=excluded.row_type, ticket=excluded.ticket,
    lane=excluded.lane, kind=excluded.kind, pr=excluded.pr, head=excluded.head,
    outcome=excluded.outcome, text=excluded.text, question=excluded.question,
    vector=excluded.vector, question_vector=excluded.question_vector
"""


def _timestamp(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat(timespec="microseconds")


def _decode(row: sqlite3.Row) -> ModelLedgerSimilarityRow:
    fields = {key: row[key] for key in _ROW_COLUMNS.split(", ")}
    fields["ticket"] = json.loads(fields["ticket"])
    return ModelLedgerSimilarityRow.model_validate(fields)


def open_index(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        try:
            file = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(file)
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def index_model(conn: sqlite3.Connection) -> tuple[str, int]:
    row = conn.execute(
        "SELECT name, dimension FROM embedding_model WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ValueError("vector index contains no embeddings")
    return str(row["name"]), int(row["dimension"])


def write_page(
    path: Path,
    ledger_id: str,
    rows: Sequence[ModelLedgerSimilarityRow],
    vectors: dict[tuple[str, str], tuple[float, ...]],
    model: str | None,
    dimension: int | None,
) -> None:
    def packed(row_id: str, field: str) -> bytes | None:
        vector = vectors.get((row_id, field))
        return struct.pack(f"<{dimension}f", *vector) if vector is not None else None

    with closing(open_index(path, write=True)) as conn:
        conn.executescript(_SCHEMA)
        with conn:
            if model is not None and dimension is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO embedding_model VALUES (1, ?, ?)",
                    (model, dimension),
                )
                if index_model(conn) != (model, dimension):
                    raise ValueError(
                        "embedding model or dimension differs from vector index"
                    )
            conn.executemany(
                _UPSERT,
                (
                    (
                        ledger_id,
                        row.row_id,
                        _timestamp(row.row_ts),
                        row.row_type,
                        json.dumps(row.ticket),
                        row.lane,
                        row.kind,
                        row.pr,
                        row.head,
                        row.outcome,
                        row.text,
                        row.question,
                        packed(row.row_id, "text"),
                        packed(row.row_id, "question"),
                    )
                    for row in rows
                ),
            )


def candidates(
    conn: sqlite3.Connection, request: ModelLedgerSimilarityRequest, *, ruling: bool
) -> Iterator[tuple[ModelLedgerSimilarityRow, bytes | None, bytes | None]]:
    predicate = (
        "row_type = 'RULING'"
        if ruling
        else "(row_type = 'FRICTION' OR (row_type = 'TERMINAL' "
        "AND outcome IN ('blocked', 'failed', 'refused')))"
    )
    params = [request.ledger_id, _timestamp(request.before)]
    conditions = [
        "ledger_id = ?",
        "row_ts < ?",
        "(vector IS NOT NULL OR question_vector IS NOT NULL)",
        predicate,
    ]
    for field in _TAG_COLUMNS:
        value = getattr(request.tags, field)
        if value is not None:
            conditions.append(f"{field} = ?")
            params.append(value)
    if request.tags.ticket is not None:
        conditions.append("? IN (SELECT value FROM json_each(ticket))")
        params.append(request.tags.ticket)
    sql = " AND ".join(conditions)
    for row in conn.execute(
        f"SELECT {_ROW_COLUMNS}, vector, question_vector FROM ledger_vector WHERE {sql}",
        params,
    ):
        yield _decode(row), row["vector"], row["question_vector"]


def next_terminal(
    conn: sqlite3.Connection,
    request: ModelLedgerSimilarityRequest,
    blocker: ModelLedgerSimilarityRow,
) -> ModelLedgerSimilarityRow | None:
    if blocker.lane is None:
        return None
    row = conn.execute(
        f"""
        SELECT {_ROW_COLUMNS} FROM ledger_vector
        WHERE ledger_id = ? AND lane = ? AND row_type = 'TERMINAL'
            AND outcome IS NOT NULL AND row_ts > ? AND row_ts < ?
        ORDER BY row_ts, row_id LIMIT 1
        """,
        (
            request.ledger_id,
            blocker.lane,
            _timestamp(blocker.row_ts),
            _timestamp(request.before),
        ),
    ).fetchone()
    return _decode(row) if row is not None else None
