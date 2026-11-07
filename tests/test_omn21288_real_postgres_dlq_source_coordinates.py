# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21288: a write Postgres refuses is dead-lettered with its source record.

The redrive for refused projection events re-reads each refused record from its
source topic once the cause is fixed. It never resets a consumer group's offset,
so the dead-letter envelope itself must say where the record lives: the
canonical source topic, its partition and its offset.

Why real Postgres: the refusal that matters here is the database's own. A
``NOT NULL`` violation raised by a live server is classified POISON and routed
through ``BaseProjectionRunner._route_poison_to_dlq``; a mock adapter would only
raise what the test author typed. The runner also reads the record from a
namespaced physical topic, so this pins that the envelope carries the canonical
name the redrive resolves through the same namespace seam the runner uses.

Harness pattern mirrors ``tests/test_omn18891_real_postgres_topic_namespace_write_path.py``:
SKIPS rather than ERRORs without a reachable database, and provisions a
throwaway schema so concurrent runs never collide.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.topic_namespace import TOPIC_NAMESPACE_ENV_VAR

CANONICAL_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"
SLOT = "prepr1"
PARTITION = 2
OFFSET = 36340

_DB_SECRET_ENV: tuple[str, ...] = (
    "INTEGRATION_POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD",
)


def _resolve_db_secret() -> str:
    for name in _DB_SECRET_ENV:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _base_dsn() -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    auth = f"{quote_plus(user)}:{quote_plus(_resolve_db_secret())}"
    return f"postgresql://{auth}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not _resolve_db_secret():
        pytest.skip(
            "no database credential in the environment -- skipping the "
            "OMN-21288 real-Postgres dead-letter source gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-21288 gate: {exc}")


class _RefusedWriteRunner(BaseProjectionRunner):
    """The smallest real runner whose write Postgres refuses."""

    def __init__(self, db: AsyncpgAdapter, schema: str) -> None:
        super().__init__()
        self._db = db
        self._schema = schema
        self.dead_letters: list[tuple[str, bytes]] = []

    @property
    def topics(self) -> list[str]:
        return [CANONICAL_TOPIC]

    @property
    def poison_dlq_topics(self) -> list[str]:
        return [DLQ_TOPIC]

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        self.dead_letters.append((topic, value))

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._db.execute(
            f"INSERT INTO {self._schema}.refused_probe (correlation_id, subject) "
            "VALUES ($1, $2)",
            str(data.get("correlation_id")),
            data.get("subject"),
        )
        return True


@asynccontextmanager
async def _provisioned() -> AsyncIterator[
    tuple[_RefusedWriteRunner, asyncpg.Connection, str]
]:
    conn = await _connect_or_skip()
    schema = f"omn21288_{uuid.uuid4().hex[:12]}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        await conn.execute(
            f'CREATE TABLE "{schema}".refused_probe ('
            "  correlation_id TEXT NOT NULL,"
            "  subject TEXT NOT NULL"
            ")"
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        await adapter.connect()
        try:
            yield _RefusedWriteRunner(adapter, f'"{schema}"'), conn, schema
        finally:
            await adapter.close()
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


class _Message:
    """A fetched record as aiokafka hands it over: PHYSICAL topic name."""

    def __init__(self, topic: str, payload: dict[str, Any]) -> None:
        self.topic = topic
        self.partition = PARTITION
        self.offset = OFFSET
        self.key = b"probe"
        self.value = json.dumps(payload).encode("utf-8")
        self.headers: list[tuple[str, bytes]] = []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_refused_write_is_dead_lettered_with_its_source_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    payload = {"correlation_id": "refused-1", "subject": None}

    async with _provisioned() as (runner, conn, schema):
        await runner._handle_message(_Message(f"{SLOT}.{CANONICAL_TOPIC}", payload))
        rows = await conn.fetchval(f'SELECT count(*) FROM "{schema}".refused_probe')

    assert rows == 0, "Postgres must have refused the write"
    assert len(runner.dead_letters) == 1
    topic, value = runner.dead_letters[0]
    assert topic == f"{SLOT}.{DLQ_TOPIC}"
    envelope = json.loads(value)
    assert envelope["correlation_id"] == "refused-1"
    assert envelope["source_topic"] == CANONICAL_TOPIC
    assert envelope["source_partition"] == PARTITION
    assert envelope["source_offset"] == OFFSET
