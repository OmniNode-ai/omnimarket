# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_session_content takes the ``checkpoint`` content kind (OMN-18531, TA.20a).

The omniclaude capture hook journals the written session checkpoint
(``omni.session-checkpoint/1``) as ``content_kind`` ``checkpoint``, raw and in
chunks (RULING 2026-10-15T04:24:42Z: no redaction on the bus). This projection
must store each chunk as a row, so the chunks reassemble to the file's own
sha256, and the two other consumers of the content-captured topic must leave a
checkpoint record alone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import pytest

from omnimarket.models.operator_capture import ModelOperatorPromptRecord
from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    SessionContentProjectionWriter,
)
from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
    SUBSCRIBE_TOPIC,
    fold_session_content,
)
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
    ModelSessionContentRow,
)

SESSION = "11111111-2222-3333-4444-555555555555"

#: A credential-shaped value the producer would have scrubbed before OMN-19513.
#: Raw capture means it arrives, and is stored, exactly as written.
RAW_SECRET_SHAPED = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _checkpoint_text() -> str:
    """A checkpoint file as the writer left it, larger than one capture chunk."""
    data = {
        "schema": "omni.session-checkpoint/1",
        "written_at": "2026-10-15T06:00:00Z",
        "session_lane": "orchestrator-test",
        "session_id": SESSION,
        "workflows": [
            {
                "task_id": f"task-{i}",
                "run_id": f"wf_{i:04d}",
                "script": "inline",
                "purpose": f"lane {i} " + "x" * 80,
                "status": "running",
                "role": "lane",
            }
            for i in range(12)
        ],
        "decisions": [{"text": f"token for the lab is {RAW_SECRET_SHAPED}"}],
        "deadlines": [],
        "ticks": [],
        "notes": ["multi-byte ✓ and a nul-free text"],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _wire_chunks(text: str, *, size: int) -> list[dict[str, Any]]:
    """The records the fan-out publishes for one checkpoint, one per chunk."""
    parts = _chunks(text, size)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return [
        {
            "session_id": SESSION,
            "turn_id": None,
            "correlation_id": SESSION,
            "emitted_at": "2026-10-15T06:00:01.000000+00:00",
            "hook_source": "checkpoint",
            "actor": "claude",
            "content_kind": "checkpoint",
            "tool_name": None,
            "tool_use_id": None,
            "chunk_index": index,
            "chunk_count": len(parts),
            "content_sha256": digest,
            "original_chars": len(text),
            "truncated": False,
            "producer_redaction": {},
            "content": part,
            "redaction_state": "passthrough",
        }
        for index, part in enumerate(parts)
    ]


def _rows(text: str, *, size: int) -> list[ModelSessionContentRow]:
    return [
        fold_session_content(ModelSessionContentRecord.model_validate(wire))
        for wire in _wire_chunks(text, size=size)
    ]


def _reassembled(rows: list[ModelSessionContentRow]) -> str:
    return "".join(r.content for r in sorted(rows, key=lambda r: r.chunk_index))


class _RecordingDb:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append((query, args))
        return "INSERT 0 1"


@pytest.mark.unit
def test_a_single_chunk_checkpoint_folds_to_one_checkpoint_row() -> None:
    text = _checkpoint_text()
    (row,) = _rows(text, size=len(text))
    assert row.content_kind == "checkpoint"
    assert row.hook_source == "checkpoint"
    assert row.session_id == SESSION
    assert row.chunk_count == 1
    assert row.content == text


@pytest.mark.unit
def test_a_checkpoint_larger_than_one_chunk_reassembles_to_the_files_sha256() -> None:
    text = _checkpoint_text()
    rows = _rows(text, size=700)
    assert len(rows) > 1
    assert [r.chunk_index for r in rows] == list(range(len(rows)))
    assert len({r.event_id for r in rows}) == len(rows)
    reassembled = _reassembled(rows)
    assert reassembled == text
    file_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert hashlib.sha256(reassembled.encode("utf-8")).hexdigest() == file_sha
    assert {r.content_sha256 for r in rows} == {file_sha}


@pytest.mark.unit
def test_the_hash_check_is_not_vacuous_a_changed_chunk_breaks_it() -> None:
    """Positive control: the reassembly comparison above does fail on a bad row."""
    text = _checkpoint_text()
    wires = _wire_chunks(text, size=700)
    wires[1] = {**wires[1], "content": wires[1]["content"].replace("lane", "LANE", 1)}
    rows = [
        fold_session_content(ModelSessionContentRecord.model_validate(w)) for w in wires
    ]
    digest = hashlib.sha256(_reassembled(rows).encode("utf-8")).hexdigest()
    assert digest != rows[0].content_sha256


@pytest.mark.unit
def test_a_credential_shaped_value_is_stored_as_written_not_rewritten() -> None:
    text = _checkpoint_text()
    reassembled = _reassembled(_rows(text, size=700))
    assert RAW_SECRET_SHAPED in reassembled
    assert "[REDACTED" not in reassembled


@pytest.mark.unit
def test_a_replayed_checkpoint_chunk_lands_on_the_same_key() -> None:
    text = _checkpoint_text()
    first = _rows(text, size=700)
    again = _rows(text, size=700)
    assert [r.event_id for r in first] == [r.event_id for r in again]


@pytest.mark.unit
def test_the_checkpoint_kind_does_not_share_a_key_with_another_kind() -> None:
    text = _checkpoint_text()
    (as_checkpoint,) = _rows(text, size=len(text))
    other = ModelSessionContentRecord.model_validate(
        {**_wire_chunks(text, size=len(text))[0], "content_kind": "prompt"}
    )
    assert fold_session_content(other).event_id != as_checkpoint.event_id


@pytest.mark.unit
def test_the_writer_upserts_every_checkpoint_chunk() -> None:
    text = _checkpoint_text()
    wires = _wire_chunks(text, size=700)
    writer = SessionContentProjectionWriter()
    db = _RecordingDb()
    cast(Any, writer)._db = db

    outs = [writer.handle({**wire, "_topic": SUBSCRIBE_TOPIC}) for wire in wires]

    assert [o["rows_upserted"] for o in outs] == [1] * len(wires)
    assert len(db.statements) == len(wires)
    stored = "".join(args[9] for _, args in db.statements)
    assert stored == text


@pytest.mark.unit
def test_the_operator_capture_reader_does_not_take_a_checkpoint_record() -> None:
    (wire,) = _wire_chunks(_checkpoint_text(), size=10**6)
    assert not ModelOperatorPromptRecord.model_validate(wire).is_operator_prompt
