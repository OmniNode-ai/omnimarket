# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The observer's on-disk journal: every event is written here before it is sent.

Events are appended in order with a sequence number, sent oldest first, and
removed only once the broker has accepted them. When the broker is unreachable
the flush stops at the first failure and the rest stays on disk, so a later
poll sends the backlog before anything newer and the order the events were
observed in is the order they arrive in.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr


class ModelJournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    topic: NonEmptyStr
    key: NonEmptyStr
    event_id: NonEmptyStr
    payload: dict[str, object]


class ProtocolObserverEventSink(Protocol):
    """The broker end: publish one event or raise when the broker will not take it."""

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None: ...


def event_id_for(topic: str, payload: dict[str, object]) -> str:
    """Content-addressed, so a replayed event carries the id it had before."""
    canonical = json.dumps([topic, payload], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventJournal:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.corrupt_lines = 0

    def append(self, entries: list[ModelJournalEntry]) -> None:
        if not entries:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(entry.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def pending(self) -> list[ModelJournalEntry]:
        if not self._path.exists():
            return []
        entries: list[ModelJournalEntry] = []
        corrupt = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(ModelJournalEntry.model_validate_json(line))
            except ValidationError:
                corrupt += 1
        self.corrupt_lines = corrupt
        return sorted(entries, key=lambda entry: entry.seq)

    def flush(self, sink: ProtocolObserverEventSink) -> tuple[int, int, str | None]:
        """Send pending events oldest first; returns (sent, left, error)."""
        pending = self.pending()
        sent = 0
        error: str | None = None
        for entry in pending:
            try:
                sink.publish(
                    entry.topic, entry.payload, key=entry.key, event_id=entry.event_id
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            sent += 1
        self._rewrite(pending[sent:])
        return sent, len(pending) - sent, error

    def _rewrite(self, remaining: list[ModelJournalEntry]) -> None:
        if not self._path.exists():
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            "".join(entry.model_dump_json() + "\n" for entry in remaining),
            encoding="utf-8",
        )
        tmp.replace(self._path)
