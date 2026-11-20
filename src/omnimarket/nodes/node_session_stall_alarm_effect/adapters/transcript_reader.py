# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the queue rows of the newest session transcript under a glob (OMN-25405).

A transcript is JSON lines. A ``queue-operation`` row carries ``operation`` (enqueue, dequeue or remove) and
``timestamp``; an enqueue's ``content`` holds the queued input. The content is matched against the overlay's task
markers and then dropped: it is never kept, logged or printed. Only the tail of the file is read, and the line the
cut falls inside is discarded.
"""

from __future__ import annotations

import glob as globbing
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.session_stall import EnumQueueOperation, ModelQueueRow
from omnimarket.nodes.node_session_stall_alarm_effect.protocols import (
    TranscriptUnreadableError,
)

_QUEUE_ROW = "queue-operation"
_OPERATIONS = {
    "enqueue": EnumQueueOperation.ENQUEUE,
    "dequeue": EnumQueueOperation.DEQUEUE,
    "remove": EnumQueueOperation.REMOVE,
}


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class FileTranscriptReader:
    def __init__(
        self,
        *,
        glob: str,
        tail_bytes: int,
        window_seconds: int,
        task_markers: tuple[str, ...],
    ) -> None:
        self._glob = glob
        self._tail = tail_bytes
        self._window = timedelta(seconds=window_seconds)
        self._markers = task_markers

    def _newest(self) -> Path:
        found = [Path(p) for p in globbing.glob(os.path.expanduser(self._glob))]
        files = [p for p in found if p.is_file()]
        if not files:
            raise TranscriptUnreadableError(
                "no transcript file matches the overlay's glob"
            )
        return max(files, key=lambda p: p.stat().st_mtime)

    def _lines(self, path: Path) -> list[bytes]:
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - self._tail)
                handle.seek(start)
                data = handle.read()
        except OSError as exc:
            raise TranscriptUnreadableError(
                f"the transcript could not be read: {type(exc).__name__}"
            ) from exc
        lines = data.split(b"\n")
        return lines[1:] if start > 0 else lines

    def read(self, now: datetime) -> tuple[ModelQueueRow, ...]:
        floor = now - self._window
        rows: list[ModelQueueRow] = []
        for raw in self._lines(self._newest()):
            if _QUEUE_ROW.encode() not in raw:
                continue
            try:
                document = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(document, dict) or document.get("type") != _QUEUE_ROW:
                continue
            operation = _OPERATIONS.get(str(document.get("operation")))
            moment = _parse_time(str(document.get("timestamp", "")))
            if operation is None or moment is None or moment < floor:
                continue
            rows.append(ModelQueueRow(at=moment, operation=operation))
            content = document.get("content")
            if (
                operation is EnumQueueOperation.ENQUEUE
                and isinstance(content, str)
                and any(marker in content for marker in self._markers)
            ):
                rows.append(
                    ModelQueueRow(at=moment, operation=EnumQueueOperation.TASK_FIRED)
                )
        return tuple(rows)


__all__ = ["FileTranscriptReader"]
