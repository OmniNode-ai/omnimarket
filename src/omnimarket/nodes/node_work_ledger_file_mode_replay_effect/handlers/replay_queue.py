# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay queue stores: in memory, and a JSON-lines file that survives a restart.

Both are keyed by row id (the SHA-256 of the exact row), so queueing a row twice, or
a row already replayed, changes nothing. The overlay names the backend and the file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    ModelQueuedRow,
    ModelReplayOverlay,
)

OVERLAY_ID = "node_work_ledger_file_mode_replay_effect"


class ProtocolReplayQueue(Protocol):
    def enqueue(self, row_id: str, row: str) -> bool:
        """Queue a row; False when its id is already known (any state)."""
        ...

    def pending(self) -> Sequence[ModelQueuedRow]: ...

    def mark_replayed(self, row_id: str) -> None: ...

    def mark_refused(self, row_id: str, reason: str) -> None: ...

    def record_attempt(self, row_id: str, reason: str) -> int:
        """Count a failed attempt, keep the row pending, return the attempts so far."""
        ...

    def pending_row_ids(self) -> set[str]: ...

    def replayed_row_ids(self) -> set[str]:
        """Return the ids of the rows already replayed."""

    def refused_reasons(self) -> dict[str, str]: ...


class InMemoryReplayQueue:
    def __init__(self) -> None:
        self._rows: dict[str, ModelQueuedRow] = {}

    def enqueue(self, row_id: str, row: str) -> bool:
        if row_id in self._rows:
            return False
        self._rows[row_id] = ModelQueuedRow(row_id=row_id, row=row)
        return True

    def pending(self) -> list[ModelQueuedRow]:
        return [r for r in self._rows.values() if r.state == "pending"]

    def mark_replayed(self, row_id: str) -> None:
        self._rows[row_id] = self._rows[row_id].model_copy(update={"state": "replayed"})

    def mark_refused(self, row_id: str, reason: str) -> None:
        self._rows[row_id] = self._rows[row_id].model_copy(
            update={"state": "refused", "last_reason": reason}
        )

    def record_attempt(self, row_id: str, reason: str) -> int:
        current = self._rows[row_id]
        updated = current.model_copy(
            update={"attempts": current.attempts + 1, "last_reason": reason}
        )
        self._rows[row_id] = updated
        return updated.attempts

    def pending_row_ids(self) -> set[str]:
        return {r.row_id for r in self.pending()}

    def replayed_row_ids(self) -> set[str]:
        return {r.row_id for r in self._rows.values() if r.state == "replayed"}

    def refused_reasons(self) -> dict[str, str]:
        return {
            r.row_id: r.last_reason or ""
            for r in self._rows.values()
            if r.state == "refused"
        }


class JsonlReplayQueue(InMemoryReplayQueue):
    """The same queue persisted as one JSON line per row, rewritten atomically."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    queued = ModelQueuedRow.model_validate(json.loads(line))
                    self._rows[queued.row_id] = queued

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text(
            "".join(r.model_dump_json() + "\n" for r in self._rows.values()),
            encoding="utf-8",
        )
        os.replace(temp, self._path)

    def enqueue(self, row_id: str, row: str) -> bool:
        added = super().enqueue(row_id, row)
        if added:
            self._flush()
        return added

    def mark_replayed(self, row_id: str) -> None:
        super().mark_replayed(row_id)
        self._flush()

    def mark_refused(self, row_id: str, reason: str) -> None:
        super().mark_refused(row_id, reason)
        self._flush()

    def record_attempt(self, row_id: str, reason: str) -> int:
        attempts = super().record_attempt(row_id, reason)
        self._flush()
        return attempts


# The overlay fields each backend needs; the backend name selects a row, and no code
# path compares it with a literal.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {"inmemory": (), "file": ("queue_path",)}


def load_replay_overlay(*, explicit: object | None = None) -> ModelReplayOverlay:
    """Read the overlay; a backend missing a field it needs is refused."""
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelReplayOverlay, explicit=explicit
        )
    except NodeOverlayError as exc:
        raise ValueError(str(exc)) from None
    missing = [
        name for name in _REQUIRED_FIELDS[overlay.backend] if not getattr(overlay, name)
    ]
    if missing:
        raise ValueError(f"backend {overlay.backend} needs overlay fields {missing}")
    return overlay


def _inmemory_queue(overlay: ModelReplayOverlay) -> InMemoryReplayQueue:
    return InMemoryReplayQueue()


def _file_queue(overlay: ModelReplayOverlay) -> InMemoryReplayQueue:
    if not overlay.queue_path:
        raise ValueError("backend file needs the overlay field queue_path")
    return JsonlReplayQueue(Path(overlay.queue_path))


_QUEUE_BUILDERS: dict[str, Callable[[ModelReplayOverlay], InMemoryReplayQueue]] = {
    "inmemory": _inmemory_queue,
    "file": _file_queue,
}


def build_replay_queue(overlay: ModelReplayOverlay) -> InMemoryReplayQueue:
    return _QUEUE_BUILDERS[overlay.backend](overlay)


__all__ = [
    "OVERLAY_ID",
    "InMemoryReplayQueue",
    "JsonlReplayQueue",
    "ProtocolReplayQueue",
    "build_replay_queue",
    "load_replay_overlay",
]
