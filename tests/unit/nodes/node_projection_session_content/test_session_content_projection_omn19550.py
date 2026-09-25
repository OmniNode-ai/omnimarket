# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_session_content (OMN-19550 AC5).

One content record on ``onex.evt.omniclaude.content-captured.v1`` is one row
in ``omninode_internal.session_content``. The key is content-addressed, so a
redelivery or a replay lands on the same row. The node is two classes (rule
7a, OMN-18769): a pure fold a unit test can falsify, and an in-process writer
the runtime calls with ``_db`` and ``_topic`` injected.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    HandlerProjectionSessionContent,
    SessionContentProjectionWriter,
)
from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
    SUBSCRIBE_TOPIC,
    SessionContentFoldError,
    fold_session_content,
)
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
    ModelSessionContentRow,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_session_content"
)
TOPIC = "onex.evt.omniclaude.content-captured.v1"


def _wire(**overrides: Any) -> dict[str, Any]:
    """A record as the fan-out publishes it, after redact_capture ran."""
    record: dict[str, Any] = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "turn_id": "11111111-2222-3333-4444-555555555555:turn-4",
        "correlation_id": "11111111-2222-3333-4444-555555555555",
        "causation_id": None,
        "emitted_at": "2026-09-25T11:40:00.123456+00:00",
        "entity_id": "e-1",
        "schema_version": "1.0.0",
        "hook_source": "post_tool_use",
        "actor": "claude",
        "content_kind": "tool_response",
        "tool_name": "Bash",
        "tool_use_id": "toolu_01ABC",
        "chunk_index": 0,
        "chunk_count": 1,
        "content_sha256": "a" * 64,
        "original_chars": 11,
        "truncated": False,
        "producer_redaction": {"github_token": 1},
        "content": "hello [REDACTED:github_token] world",
        "command": "gh auth status",
        "redaction_state": "secret_detected",
        "lane": "full-content-capture-83",
    }
    record.update(overrides)
    return record


class _RecordingDb:
    """A stand-in for the asyncpg adapter: records statements, owns no pool."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append((query, args))
        return "INSERT 0 1"


# ---------------------------------------------------------------------------
# the pure fold
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_contract_subscribes_exactly_the_content_topic() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [TOPIC]
    assert SUBSCRIBE_TOPIC == TOPIC


@pytest.mark.unit
def test_one_record_folds_to_one_row_with_every_join_key() -> None:
    row = fold_session_content(ModelSessionContentRecord.model_validate(_wire()))
    assert isinstance(row, ModelSessionContentRow)
    assert row.session_id == _wire()["session_id"]
    assert row.turn_id == _wire()["turn_id"]
    assert row.correlation_id == _wire()["correlation_id"]
    assert row.tool_use_id == "toolu_01ABC"
    assert row.content_kind == "tool_response"
    assert row.content == "hello [REDACTED:github_token] world"
    assert row.producer_redaction == {"github_token": 1}
    assert row.emitted_at == datetime(2026, 9, 25, 11, 40, 0, 123456, tzinfo=UTC)
    assert row.source_topic == TOPIC


@pytest.mark.unit
def test_the_key_is_content_addressed_and_a_replay_lands_on_it() -> None:
    first = fold_session_content(ModelSessionContentRecord.model_validate(_wire()))
    again = fold_session_content(ModelSessionContentRecord.model_validate(_wire()))
    assert first.event_id == again.event_id
    assert len(first.event_id) == 64


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"chunk_index": 1},
        {"tool_use_id": "toolu_02XYZ"},
        {"turn_id": "11111111-2222-3333-4444-555555555555:turn-5"},
        {"content_kind": "tool_input"},
        {"content_sha256": "b" * 64},
        {"emitted_at": "2026-09-25T11:40:01+00:00"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_distinct_content_items_get_distinct_keys(change: dict[str, Any]) -> None:
    base = fold_session_content(ModelSessionContentRecord.model_validate(_wire()))
    other = fold_session_content(
        ModelSessionContentRecord.model_validate(_wire(**change))
    )
    assert base.event_id != other.event_id


@pytest.mark.unit
def test_a_nul_character_postgres_cannot_store_is_replaced_not_dropped() -> None:
    row = fold_session_content(
        ModelSessionContentRecord.model_validate(_wire(content="a\x00b"))
    )
    assert row.content == "a�b"


@pytest.mark.unit
def test_a_hashed_content_field_projects_as_the_digest_it_arrived_as() -> None:
    digest = "sha256:" + "c" * 64
    row = fold_session_content(
        ModelSessionContentRecord.model_validate(_wire(content=digest, command=digest))
    )
    assert row.content == digest


@pytest.mark.unit
def test_a_record_with_no_session_is_refused() -> None:
    with pytest.raises(SessionContentFoldError, match="session_id"):
        fold_session_content(
            ModelSessionContentRecord.model_validate(_wire(session_id=""))
        )


@pytest.mark.unit
def test_the_pure_handler_is_definition_b_and_touches_no_database() -> None:
    record = ModelSessionContentRecord.model_validate(_wire())
    row = HandlerProjectionSessionContent().handle(record)
    assert row == fold_session_content(record)


# ---------------------------------------------------------------------------
# the in-process writer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_writer_declares_in_process_dispatch() -> None:
    assert SessionContentProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.unit
def test_the_writer_upserts_one_row_and_reports_it_to_the_runtime() -> None:
    writer = SessionContentProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]

    out = writer.handle({**_wire(), "_topic": TOPIC, "_envelope_id": "env-1"})

    assert out["rows_upserted"] == 1
    assert db.connected == db.closed == 1
    ((query, args),) = db.statements
    assert "omninode_internal.session_content" in query
    assert "ON CONFLICT (event_id)" in query
    expected = fold_session_content(ModelSessionContentRecord.model_validate(_wire()))
    assert args[0] == expected.event_id
    assert expected.content in args


@pytest.mark.unit
def test_a_replay_through_the_writer_upserts_the_same_key() -> None:
    writer = SessionContentProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]
    writer.handle({**_wire(), "_topic": TOPIC})
    writer.handle({**_wire(), "_topic": TOPIC})
    assert db.statements[0][1][0] == db.statements[1][1][0]


@pytest.mark.unit
def test_the_writer_refuses_a_topic_it_does_not_subscribe() -> None:
    writer = SessionContentProjectionWriter()
    writer._db = _RecordingDb()  # type: ignore[assignment]
    with pytest.raises(SessionContentFoldError, match="not subscribed"):
        writer.handle({**_wire(), "_topic": "onex.evt.omniclaude.tool-executed.v1"})


@pytest.mark.unit
def test_the_writer_works_when_called_from_inside_a_running_loop() -> None:
    writer = SessionContentProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]

    async def call() -> dict[str, Any]:
        return writer.handle({**_wire(), "_topic": TOPIC})

    assert asyncio.run(call())["rows_upserted"] == 1


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_migration_creates_the_table_the_writer_names_and_grants_no_delete() -> (
    None
):
    sql = (NODE_DIR / "migrations" / "0001_create_session_content.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS omninode_internal.session_content" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON omninode_internal.session_content" in sql
    assert "DELETE" not in sql.split("GRANT", 1)[1].split(";", 1)[0]
    row_columns = set(ModelSessionContentRow.model_fields)
    for column in row_columns:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, column
