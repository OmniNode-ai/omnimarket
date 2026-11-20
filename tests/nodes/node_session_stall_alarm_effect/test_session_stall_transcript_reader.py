# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The transcript reader turns queue-operation rows into the judge's rows and prints no content (OMN-25405)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.session_stall import EnumQueueOperation
from omnimarket.nodes.node_session_stall_alarm_effect.adapters.transcript_reader import (
    FileTranscriptReader,
)
from omnimarket.nodes.node_session_stall_alarm_effect.protocols import (
    TranscriptUnreadableError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
MARKER = "<scheduled-task"


def _line(kind: str, at: datetime, **extra: object) -> str:
    row = {
        "type": kind,
        "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "sessionId": "s",
        **extra,
    }
    return json.dumps(row)


def _reader(glob: str, **kw: int) -> FileTranscriptReader:
    return FileTranscriptReader(
        glob=glob,
        tail_bytes=kw.get("tail_bytes", 1_000_000),
        window_seconds=kw.get("window_seconds", 6 * 3600),
        task_markers=(MARKER,),
    )


def test_the_reader_maps_enqueue_dequeue_remove_and_a_scheduled_task(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    base = NOW - timedelta(hours=1)
    path.write_text(
        "\n".join(
            [
                _line("user", base),
                _line(
                    "queue-operation",
                    base + timedelta(minutes=1),
                    operation="enqueue",
                    content="a notice",
                ),
                _line(
                    "queue-operation",
                    base + timedelta(minutes=2),
                    operation="enqueue",
                    content=f"{MARKER} name=tick>",
                ),
                _line(
                    "queue-operation", base + timedelta(minutes=3), operation="dequeue"
                ),
                _line(
                    "queue-operation",
                    base + timedelta(minutes=4),
                    operation="remove",
                    content="x",
                    reason="r",
                ),
                "not json at all",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rows = _reader(str(tmp_path / "*.jsonl")).read(NOW)
    assert [r.operation for r in rows] == [
        EnumQueueOperation.ENQUEUE,
        EnumQueueOperation.ENQUEUE,
        EnumQueueOperation.TASK_FIRED,
        EnumQueueOperation.DEQUEUE,
        EnumQueueOperation.REMOVE,
    ]
    assert rows[0].at == base + timedelta(minutes=1)


def test_the_reader_keeps_only_the_window_and_the_newest_file(tmp_path: Path) -> None:
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text(
        _line("queue-operation", NOW - timedelta(minutes=5), operation="enqueue") + "\n"
    )
    new.write_text(
        _line("queue-operation", NOW - timedelta(hours=9), operation="enqueue")
        + "\n"
        + _line("queue-operation", NOW - timedelta(minutes=9), operation="dequeue")
        + "\n"
    )
    os.utime(old, (1, 1))
    rows = _reader(str(tmp_path / "*.jsonl")).read(NOW)
    assert [r.operation for r in rows] == [EnumQueueOperation.DEQUEUE]


def test_the_reader_reads_the_tail_and_drops_the_cut_line(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    filler = _line("user", NOW - timedelta(hours=1), content="x" * 5000)
    tail = _line("queue-operation", NOW - timedelta(minutes=1), operation="enqueue")
    path.write_text((filler + "\n") * 20 + tail + "\n")
    rows = _reader(str(path), tail_bytes=len(tail) + 40).read(NOW)
    assert [r.operation for r in rows] == [EnumQueueOperation.ENQUEUE]


def test_no_file_is_an_unreadable_transcript(tmp_path: Path) -> None:
    with pytest.raises(TranscriptUnreadableError):
        _reader(str(tmp_path / "*.jsonl")).read(NOW)
