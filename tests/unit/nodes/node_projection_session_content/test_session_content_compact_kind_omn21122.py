# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_session_content takes the ``compact`` content kind (OMN-21122, TA.20b).

The omniclaude capture hook journals the installed compaction (the transcript's
newest ``compact_boundary`` record and every record after it) as
``content_kind`` ``compact``, raw and in chunks (RULING 2026-10-15T04:24:42Z: no
redaction on the bus). This projection must store each chunk as a row, so the
chunks reassemble to the package's own sha256 and still hold the boundary's
``uuid`` and every replacement record, and the other consumer of the
content-captured topic must leave a compact record alone.

The package is read from a transcript by node_compact_package_read_effect's
``compact_items``, the same call the PostCompact capture makes, so these rows
are checked against what the producer actually publishes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.models.operator_capture import ModelOperatorPromptRecord
from omnimarket.nodes.node_compact_package_read_effect.handlers.handler_compact_package_read import (
    compact_items,
)
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


BOUNDARY_UUID = "783e1d6e-1111-2222-3333-444444444444"
OLDER_BOUNDARY_UUID = "783e1d6e-0000-0000-0000-000000000000"
EARLIER_UUID = "99990000-1111-0000-0000-000000000000"
REPLACEMENT_UUIDS = tuple(f"99990000-0000-0000-0000-{n:012d}" for n in range(12))


def _boundary(uuid: str) -> str:
    return (
        '{"parentUuid": null, "type": "system", "subtype": "compact_boundary", '
        f'"uuid": "{uuid}", "compactMetadata": {{"trigger": "manual"}}}}'
    )


def _compact_text() -> str:
    """A compaction package as the capture reads it, larger than one chunk.

    The transcript holds an earlier compaction and a record before the newest
    boundary; the reader returns the newest boundary and every record after it.
    """
    records = [
        json.dumps(
            {
                "type": "user" if n % 2 == 0 else "assistant",
                "uuid": uuid,
                "message": {"content": f"kept record {n} " + "x" * 80},
            }
        )
        for n, uuid in enumerate(REPLACEMENT_UUIDS)
    ]
    records.append(json.dumps({"type": "user", "token": RAW_SECRET_SHAPED}))
    earlier = [
        _boundary(OLDER_BOUNDARY_UUID),
        json.dumps({"type": "user", "uuid": EARLIER_UUID, "message": "before"}),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / f"{SESSION}.jsonl"
        transcript.write_text(
            "\n".join([*earlier, _boundary(BOUNDARY_UUID), *records]) + "\n",
            encoding="utf-8",
        )
        (item,) = compact_items({"transcript_path": str(transcript)})
    assert item["content_kind"] == "compact"
    text = str(item["content"])
    assert text == "\n".join([_boundary(BOUNDARY_UUID), *records]) + "\n"
    return text


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _wire_chunks(text: str, *, size: int) -> list[dict[str, Any]]:
    """The records the fan-out publishes for one compaction, one per chunk."""
    parts = _chunks(text, size)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return [
        {
            "session_id": SESSION,
            "turn_id": None,
            "correlation_id": SESSION,
            "emitted_at": "2026-10-15T06:00:01.000000+00:00",
            "hook_source": "post_compact",
            "actor": "claude",
            "content_kind": "compact",
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
def test_a_single_chunk_compaction_folds_to_one_compact_row() -> None:
    text = _compact_text()
    (row,) = _rows(text, size=len(text))
    assert row.content_kind == "compact"
    assert row.hook_source == "post_compact"
    assert row.session_id == SESSION
    assert row.chunk_count == 1
    assert row.content == text


@pytest.mark.unit
def test_a_compaction_larger_than_one_chunk_reassembles_to_the_packages_sha256() -> (
    None
):
    text = _compact_text()
    rows = _rows(text, size=700)
    assert len(rows) > 1
    assert [r.chunk_index for r in rows] == list(range(len(rows)))
    assert len({r.event_id for r in rows}) == len(rows)
    reassembled = _reassembled(rows)
    assert reassembled == text
    assert BOUNDARY_UUID in reassembled
    assert all(uuid in reassembled for uuid in REPLACEMENT_UUIDS)
    assert OLDER_BOUNDARY_UUID not in reassembled
    assert EARLIER_UUID not in reassembled
    file_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert hashlib.sha256(reassembled.encode("utf-8")).hexdigest() == file_sha
    assert {r.content_sha256 for r in rows} == {file_sha}


@pytest.mark.unit
def test_the_hash_check_is_not_vacuous_a_changed_chunk_breaks_it() -> None:
    """Positive control: the reassembly comparison above does fail on a bad row."""
    text = _compact_text()
    wires = _wire_chunks(text, size=700)
    wires[1] = {**wires[1], "content": wires[1]["content"].replace("kept", "KEPT", 1)}
    rows = [
        fold_session_content(ModelSessionContentRecord.model_validate(w)) for w in wires
    ]
    digest = hashlib.sha256(_reassembled(rows).encode("utf-8")).hexdigest()
    assert digest != rows[0].content_sha256


@pytest.mark.unit
def test_a_credential_shaped_value_is_stored_as_written_not_rewritten() -> None:
    text = _compact_text()
    reassembled = _reassembled(_rows(text, size=700))
    assert RAW_SECRET_SHAPED in reassembled
    assert "[REDACTED" not in reassembled


@pytest.mark.unit
def test_a_replayed_compact_chunk_lands_on_the_same_key() -> None:
    text = _compact_text()
    first = _rows(text, size=700)
    again = _rows(text, size=700)
    assert [r.event_id for r in first] == [r.event_id for r in again]


@pytest.mark.unit
def test_the_compact_kind_does_not_share_a_key_with_another_kind() -> None:
    text = _compact_text()
    (as_compact,) = _rows(text, size=len(text))
    other = ModelSessionContentRecord.model_validate(
        {**_wire_chunks(text, size=len(text))[0], "content_kind": "prompt"}
    )
    assert fold_session_content(other).event_id != as_compact.event_id


@pytest.mark.unit
def test_the_writer_upserts_every_compact_chunk() -> None:
    text = _compact_text()
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
def test_the_operator_capture_reader_does_not_take_a_compact_record() -> None:
    (wire,) = _wire_chunks(_compact_text(), size=10**6)
    assert not ModelOperatorPromptRecord.model_validate(wire).is_operator_prompt
