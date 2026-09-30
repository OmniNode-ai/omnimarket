# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Real-Postgres write-path gate for the session content projection (OMN-19550).

A database double accepts an ISO string for a TIMESTAMPTZ and any text for a
JSONB parameter. Only live asyncpg refuses them, which is the OMN-15905 class
this gate exists for. The node's own migration is applied into a throwaway
``uuid4``-named schema, so concurrent runs never collide, and the test SKIPS
when no server is reachable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_session_content.handlers import (
    handler_session_content as writer_module,
)
from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    SessionContentProjectionWriter,
)

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_session_content/migrations"
    / "0001_create_session_content.sql"
)
TOPIC = "onex.cmd.omniintelligence.content-captured.v1"


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping the OMN-19550 write-path gate"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-19550 write-path gate: {exc}")
        raise AssertionError("unreachable") from exc


class _ConnectionDb:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *args: Any) -> str:
        return str(await self._connection.execute(sql, *args))

    async def connect(self) -> None:
        """No-op: bound to one live connection."""

    async def close(self) -> None:
        """No-op, for the same reason."""


@asynccontextmanager
async def _migrated_writer() -> AsyncIterator[
    tuple[SessionContentProjectionWriter, asyncpg.Connection, str]
]:
    connection = await _connect_or_skip()
    schema = f"omn19550_{uuid4().hex[:12]}"
    user = await connection.fetchval("SELECT current_user")
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        # Every reference to the lab schema and the runtime role is rebound to
        # the throwaway schema and the connecting role, so the migration's own
        # preconditions, post-conditions and grants all run for real.
        ddl = (
            MIGRATION.read_text(encoding="utf-8")
            .replace("omninode_internal", schema)
            .replace("omninode_runtime", str(user))
        )
        await connection.execute(ddl)
        writer = SessionContentProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]
        writer_module._UPSERT = original_upsert.replace(
            "omninode_internal.session_content", f"{schema}.session_content"
        )
        yield writer, connection, schema
    finally:
        writer_module._UPSERT = original_upsert
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "session_id": "pg-session",
        "turn_id": "pg-session:turn-1",
        "correlation_id": "pg-session",
        "content_kind": "tool_input",
        "tool_name": "Bash",
        "tool_use_id": "toolu_pg",
        "chunk_index": 0,
        "chunk_count": 1,
        "content": '{"command": "ls"}',
        "command": {"argv": ["ls", "-la"]},
        "content_sha256": "d" * 64,
        "original_chars": 17,
        "truncated": False,
        "producer_redaction": {"github_token": 1},
        "redaction_state": "redacted",
        "emitted_at": "2026-09-25T12:00:00.5+00:00",
    }
    record.update(overrides)
    return record


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_row_lands_with_real_column_types() -> None:
    async with _migrated_writer() as (writer, connection, schema):
        await writer._write(TOPIC, _record())
        stored = await connection.fetchrow(f"SELECT * FROM {schema}.session_content")
        assert stored is not None
        assert isinstance(stored["emitted_at"], datetime)
        assert stored["content"] == '{"command": "ls"}'
        assert stored["truncated"] is False
        assert stored["chunk_count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replay_is_one_row() -> None:
    async with _migrated_writer() as (writer, connection, schema):
        await writer._write(TOPIC, _record())
        await writer._write(TOPIC, _record())
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.session_content"
        )
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nul_character_and_a_long_chunk_are_stored() -> None:
    async with _migrated_writer() as (writer, connection, schema):
        await writer._write(TOPIC, _record(content="a\x00b" + "z" * 65000))
        stored = await connection.fetchval(
            f"SELECT content FROM {schema}.session_content"
        )
        assert stored.startswith("a�b")
        assert len(stored) == 65003
