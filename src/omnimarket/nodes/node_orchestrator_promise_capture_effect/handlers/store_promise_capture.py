# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transactional capture projection; incomplete Stop chunks survive restarts."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseCaptureRecord,
    ModelPromiseStoredCapture,
)
from omnimarket.models.operator_capture import ModelUtterance


class StorePromiseCapture:
    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS chunks (capture TEXT, idx INTEGER, record TEXT NOT NULL, PRIMARY KEY (capture, idx))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS captures (capture TEXT PRIMARY KEY, result TEXT NOT NULL)"
            )

    def close(self) -> None:
        self._db.close()

    def assemble(
        self, record: ModelPromiseCaptureRecord
    ) -> tuple[str, ModelUtterance | None]:
        source_id = record.content_record_id or record.capture_id
        stamp = record.said_at or record.emitted_at
        if not source_id:
            if not stamp:
                raise ValueError("Stop record needs a source id or source timestamp")
            identity = "\0".join(
                (
                    stamp,
                    record.correlation_id or "",
                    record.content_sha256
                    or (record.content if record.chunk_count == 1 else ""),
                )
            )
            source_id = hashlib.sha256(identity.encode()).hexdigest()
        capture_id = hashlib.sha256(
            f"{record.session_id}\0{source_id}".encode()
        ).hexdigest()
        if record.chunk_index >= record.chunk_count:
            raise ValueError("Stop chunk index exceeds its count")
        with self._lock, self._db:
            old = self._db.execute(
                "SELECT record FROM chunks WHERE capture=? AND idx=?",
                (capture_id, record.chunk_index),
            ).fetchone()
            if old is not None:
                prior = ModelPromiseCaptureRecord.model_validate_json(old[0])
                if prior != record:
                    raise ValueError("conflicting replay of a Stop chunk")
            else:
                self._db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?)",
                    (capture_id, record.chunk_index, record.model_dump_json()),
                )
            chunks = tuple(
                ModelPromiseCaptureRecord.model_validate_json(row[0])
                for row in self._db.execute(
                    "SELECT record FROM chunks WHERE capture=? ORDER BY idx",
                    (capture_id,),
                )
            )
            if any(
                (chunk.session_id, chunk.lane, chunk.chunk_count, chunk.content_sha256)
                != (
                    record.session_id,
                    record.lane,
                    record.chunk_count,
                    record.content_sha256,
                )
                for chunk in chunks
            ):
                raise ValueError("inconsistent Stop chunk metadata")
            if len(chunks) != record.chunk_count or any(
                chunk.truncated for chunk in chunks
            ):
                return capture_id, None
            text = "".join(chunk.content for chunk in chunks)
            if (
                record.content_sha256
                and hashlib.sha256(text.encode()).hexdigest() != record.content_sha256
            ):
                raise ValueError("assembled Stop content digest mismatch")
            first = chunks[0]
            stamp = first.said_at or first.emitted_at
            if not stamp:
                raise ValueError(
                    "Stop record needs its source time; ingestion time is not source time"
                )
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("Stop source time needs a timezone")
            return capture_id, ModelUtterance(
                capture_id=capture_id,
                text=text,
                session_id=first.session_id,
                source=f"content-captured:{source_id}",
                captured_at=when,
                lane=first.lane,
            )

    def get(self, capture_id: str) -> ModelPromiseStoredCapture | None:
        with self._lock:
            row = self._db.execute(
                "SELECT result FROM captures WHERE capture=?", (capture_id,)
            ).fetchone()
        return ModelPromiseStoredCapture.model_validate_json(row[0]) if row else None

    def put(self, capture: ModelPromiseStoredCapture) -> bool:
        with self._lock, self._db:
            result = self._db.execute(
                "INSERT OR IGNORE INTO captures VALUES (?, ?)",
                (capture.utterance.capture_id, capture.model_dump_json()),
            )
        return result.rowcount == 1

    def captures(self) -> tuple[ModelPromiseStoredCapture, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT result FROM captures ORDER BY capture"
            ).fetchall()
        return tuple(
            ModelPromiseStoredCapture.model_validate_json(row[0]) for row in rows
        )
