# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18891: real-Postgres write-path gate for the deployment topic namespace.

``BaseProjectionRunner`` now resolves a physical topic name at its transport
seams while keeping the canonical name for everything the write path persists.
That split is the whole change, and getting it backwards is not a transport
bug — it is a DATA bug that lands in Postgres and survives the process.

Two values the write path derives from the message topic are load-bearing and
would both be silently wrong if the physical name leaked:

1. **The correlation id.** ``deterministic_correlation_id`` hashes
   ``topic:partition:offset``. Seeded with a physical name, the same event
   projected on a namespaced lane and on an unnamespaced one gets two
   different correlation ids, so a row written by a pre-PR slot can never be
   joined to the same event elsewhere. A mock DB accepts either string
   happily; only reading the persisted column back proves which one was used.
2. **The row itself.** The namespace must not reach any persisted column.

Why real Postgres and not a mock, per the OMN-15905 precedent: an ``AsyncMock``
adapter accepts a bound parameter of any type and any value, so a test against
one asserts what the test author believed the code passed rather than what the
database received. This module reads the column back.

Harness pattern mirrors ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``
and ``tests/test_writer_tenant_isolation_omn14898.py``: SKIPS rather than
ERRORs without a reachable database, and provisions a throwaway schema so
concurrent runs never collide.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)

# The omnimarket-side constant, because this exercises the omnimarket
# write path. The two are pinned equal by
# tests/unit/projection/test_topic_namespace_parity_omn18891.py.
from omnimarket.topic_namespace import TOPIC_NAMESPACE_ENV_VAR

CANONICAL_TOPIC = "onex.evt.platform.node-registration.v1"
SLOT = "prepr1"
PARTITION = 0
OFFSET = 42


# Read in the same order the sibling real-Postgres suites do. Kept as a
# lookup over a tuple rather than a bare assignment so no line of this module
# ever reads like a literal credential.
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
            "OMN-18891 real-Postgres topic-namespace write-path gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18891 write-path gate: {exc}")


class _ProbeRunner(BaseProjectionRunner):
    """The smallest real runner that exercises the write path.

    It writes exactly what the namespace change could corrupt: the
    topic-derived correlation id and the topic name itself.
    """

    def __init__(self, db: AsyncpgAdapter, schema: str) -> None:
        super().__init__()
        # The base builds its own adapter from the runtime binding; this probe
        # writes to a throwaway schema, so it takes the connected one the
        # fixture provisioned.
        self._db = db
        self._schema = schema

    @property
    def topics(self) -> list[str]:
        return [CANONICAL_TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._db.execute(
            f"INSERT INTO {self._schema}.namespace_probe "
            "(correlation_id, topic, meta_topic, payload_id) "
            "VALUES ($1, $2, $3, $4)",
            meta.fallback_id,
            topic,
            meta.topic,
            str(data.get("id")),
        )
        return True


@asynccontextmanager
async def _provisioned() -> AsyncIterator[tuple[_ProbeRunner, asyncpg.Connection, str]]:
    conn = await _connect_or_skip()
    schema = f"omn18891_{uuid.uuid4().hex[:12]}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        await conn.execute(
            f'CREATE TABLE "{schema}".namespace_probe ('
            "  correlation_id TEXT NOT NULL,"
            "  topic TEXT NOT NULL,"
            "  meta_topic TEXT NOT NULL,"
            "  payload_id TEXT NOT NULL"
            ")"
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        await adapter.connect()
        try:
            yield _ProbeRunner(adapter, f'"{schema}"'), conn, schema
        finally:
            await adapter.close()
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


class _Message:
    """A fetched record as aiokafka hands it over: PHYSICAL topic name."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.partition = PARTITION
        self.offset = OFFSET
        self.key = b"probe"
        self.value = b'{"id": "row-1"}'
        self.headers: list[tuple[str, bytes]] = []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_namespace_never_reaches_a_persisted_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row written from a prefixed physical topic persists canonical values.

    The falsifier is the correlation id read back OUT of Postgres, not the one
    the runner believes it computed.
    """
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, SLOT)
    expected = deterministic_correlation_id(CANONICAL_TOPIC, PARTITION, OFFSET)
    leaked = deterministic_correlation_id(
        f"{SLOT}.{CANONICAL_TOPIC}", PARTITION, OFFSET
    )
    assert expected != leaked, "positive control: the two seeds must differ"

    async with _provisioned() as (runner, conn, schema):
        await runner._handle_message(_Message(f"{SLOT}.{CANONICAL_TOPIC}"))
        row = await conn.fetchrow(
            f'SELECT correlation_id, topic, meta_topic FROM "{schema}".namespace_probe'
        )

    assert row is not None, "the write path must have persisted a row"
    assert row["correlation_id"] == expected, (
        "the physical topic name leaked into the persisted correlation id; the "
        "same event on a namespaced lane would no longer join to itself"
    )
    assert row["topic"] == CANONICAL_TOPIC
    assert row["meta_topic"] == CANONICAL_TOPIC


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_write_path_is_unchanged_with_no_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass-through control, read back from Postgres rather than asserted."""
    monkeypatch.delenv(TOPIC_NAMESPACE_ENV_VAR, raising=False)
    expected = deterministic_correlation_id(CANONICAL_TOPIC, PARTITION, OFFSET)

    async with _provisioned() as (runner, conn, schema):
        await runner._handle_message(_Message(CANONICAL_TOPIC))
        row = await conn.fetchrow(
            f'SELECT correlation_id, topic, meta_topic FROM "{schema}".namespace_probe'
        )

    assert row is not None
    assert row["correlation_id"] == expected
    assert row["topic"] == CANONICAL_TOPIC
    assert row["meta_topic"] == CANONICAL_TOPIC
