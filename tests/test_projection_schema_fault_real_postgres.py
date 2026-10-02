# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that a missing column is a SCHEMA fault, not a retry loop.

On 2026-10-02 the dev-lane delegation writer needed ``routed_model`` and
``trace_id`` from ``0052_delegation_events_trace_and_routing.sql`` while the lane
database lacked them, and sat Docker-healthy for about 2h10m committing nothing.
``tests/test_projection_schema_fault_readiness.py`` proves the runner's
behaviour against a raised asyncpg exception. That is not sufficient for this
change: the identifier the runner reports is parsed out of the SERVER's own
message, and the SQLSTATE that routes the error is set by the server, so only a
real Postgres connection can show that an actual ``INSERT`` into a table that
lacks the column produces the exception, the SQLSTATE and the message the
classifier and ``schema_error_from`` expect.

Harness pattern (``_connect_or_skip`` / disposable schema) mirrors
``tests/test_omn17985_real_postgres_wedged_partition_gate.py`` -- SKIPS (never
ERRORs) without a reachable database and provisions its own throwaway schema so
runs never collide.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    ProjectionSchemaError,
    classify_projection_error,
    schema_error_from,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the real-Postgres "
            "schema-fault gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the schema-fault gate: {exc}")


@asynccontextmanager
async def _events_table(*, with_routed_model: bool) -> AsyncIterator[tuple[Any, str]]:
    """A disposable schema holding an ``events`` table, with or without the column."""
    conn = await _connect_or_skip()
    schema = f"schema_fault_{uuid4().hex[:8]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        routed = ", routed_model TEXT" if with_routed_model else ""
        await conn.execute(
            f"CREATE TABLE {schema}.events (correlation_id TEXT PRIMARY KEY{routed})"
        )
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


class _RecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Msg:
    def __init__(self, *, offset: int) -> None:
        self.topic = _TOPIC
        self.partition = 0
        self.offset = offset
        self.value = b'{"payload": {"correlation_id": "c-real"}}'


class _InsertingRunner(BaseProjectionRunner):
    """Projects by running a real INSERT that names ``routed_model``."""

    def __init__(self, conn: Any, schema: str) -> None:
        super().__init__(
            runtime_binding=ModelProjectionRuntimeBinding(
                kafka_bootstrap_servers="redpanda.test:9092",
                database_url="postgresql://p:***REDACTED***@db.test:5432/projections",
            )
        )
        self._conn = conn
        self._schema = schema
        self.consumer = _RecordingConsumer()
        self._consumer = self.consumer

    @property
    def topics(self) -> list[str]:
        return [_TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._conn.execute(
            f"INSERT INTO {self._schema}.events (correlation_id, routed_model) "
            "VALUES ($1, $2) ON CONFLICT (correlation_id) DO NOTHING",
            meta.fallback_id,
            "model-a",
        )
        return True

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        return None


@pytest.mark.integration
class TestMissingColumnAgainstRealPostgres:
    async def test_the_server_reports_42703_and_the_classifier_routes_it_schema(
        self,
    ) -> None:
        async with _events_table(with_routed_model=False) as (conn, schema):
            with pytest.raises(asyncpg.exceptions.UndefinedColumnError) as raised:
                await conn.execute(
                    f"INSERT INTO {schema}.events (correlation_id, routed_model) "
                    "VALUES ('c', 'm')"
                )

        assert raised.value.sqlstate == "42703"
        assert classify_projection_error(raised.value) is ProjectionErrorClass.SCHEMA
        error = schema_error_from(raised.value, _MIGRATIONS_DIR)
        assert error.sqlstate == "42703"
        assert error.identifier == "routed_model"
        assert "0052_delegation_events_trace_and_routing.sql" in error.migrations

    async def test_a_missing_table_is_42p01_and_names_the_relation(self) -> None:
        relation = f"no_such_relation_{uuid4().hex[:8]}"
        conn = await _connect_or_skip()
        try:
            with pytest.raises(asyncpg.exceptions.UndefinedTableError) as raised:
                await conn.execute(f"SELECT 1 FROM public.{relation}")
        finally:
            await conn.close()

        assert raised.value.sqlstate == "42P01"
        assert classify_projection_error(raised.value) is ProjectionErrorClass.SCHEMA
        assert schema_error_from(raised.value, None).identifier.endswith(relation)

    async def test_the_runner_holds_the_offset_and_fails_readiness(self) -> None:
        async with _events_table(with_routed_model=False) as (conn, schema):
            runner = _InsertingRunner(conn, schema)
            with pytest.raises(ProjectionSchemaError) as raised:
                await runner._handle_message(_Msg(offset=17))

        assert raised.value.identifier == "routed_model"
        assert runner.consumer.commits == []
        assert (_TOPIC, 0) in runner._schema_faults

    async def test_with_the_column_present_the_same_event_commits_and_no_fault_opens(
        self,
    ) -> None:
        """POSITIVE CONTROL: the write path genuinely works once the schema is there."""
        async with _events_table(with_routed_model=True) as (conn, schema):
            runner = _InsertingRunner(conn, schema)
            await runner._handle_message(_Msg(offset=17))
            rows = await conn.fetch(f"SELECT routed_model FROM {schema}.events")

        assert [row["routed_model"] for row in rows] == ["model-a"]
        assert list(runner.consumer.commits[0].values()) == [18]
        assert runner._schema_faults == {}
