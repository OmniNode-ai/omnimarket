# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20152: the projection API reads the materialized projection tables.

Operator, 2026-09-30: "One delegation row is bullshit because everything should
be gathered from projections all that information is in the fucking database."

The API used to serve from an in-memory, Kafka-fed cache that knew only the
rows it had seen since its last restart. These tests pin the replacement:

* a read is answered from the writer's table, tenant-scoped in SQL and by the
  ``app.tenant_id`` GUC, and says ``backing: table``;
* the served window is the one the cache served (the newest ``limit * 4`` rows
  by the exposure's recency column), and a ``since`` walk reads above the
  cursor with the cursor column's own type;
* the process answers with no Kafka broker at all, and a database it cannot
  reach makes a read refuse by name rather than hold startup or return an
  empty ``200``;
* ``/ready`` names each exposure whose table cannot be read, and why.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from omnimarket.config.settings import get_settings
from omnimarket.projection import api_server
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import (
    parse_order_by_clauses,
    resolve_relation_schema,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
    TableRowSource,
    build_window_query,
    dsn_env_for,
)

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_FLOW = "onex.snapshot.projection.consumer-flow.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"


def _decisions_cfg(**overrides: Any) -> ProjectionTableConfig:
    columns = ("correlation_id", "tenant_id", "written_at", "cost_usd", "payload")
    fields: dict[str, Any] = {
        "topic": _DECISIONS,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": columns,
        "json_columns": ("payload",),
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", columns),
        "freshness_column": "written_at",
        "limit": 500,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _flow_cfg(**overrides: Any) -> ProjectionTableConfig:
    columns = ("projection_cursor", "consumer_group", "window_end")
    fields: dict[str, Any] = {
        "topic": _FLOW,
        "table": "consumer_flow_windows",
        "schema_name": "omnidash_analytics",
        "relation_schema": "omninode_internal",
        "columns": columns,
        "order_by": "window_end DESC",
        "order_by_spec": parse_order_by_clauses("window_end DESC", columns),
        "freshness_column": "window_end",
        "cursor_column": "projection_cursor",
        "limit": 50,
        "bus_backed": True,
        "key_columns": ("consumer_group",),
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


# ---------------------------------------------------------------------------
# A fake asyncpg pool: records every statement, returns the rows it was given
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        self._pool.readonly.append(readonly)
        yield

    async def execute(self, sql: str, *params: Any) -> str:
        self._pool.statements.append((sql, params))
        if self._pool.raise_on_execute is not None:
            raise self._pool.raise_on_execute
        return "OK"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self._pool.statements.append((sql, params))
        return self._pool.cursor_type

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._pool.statements.append((sql, params))
        if self._pool.raise_on_fetch is not None:
            raise self._pool.raise_on_fetch
        return list(self._pool.records)


class _FakePool:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = records or []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.readonly: list[bool] = []
        self.cursor_type: str | None = "bigint"
        self.raise_on_fetch: Exception | None = None
        self.raise_on_execute: Exception | None = None

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _FakeConnection(self)

    async def close(self) -> None:
        return None


def _source_over(pool: _FakePool) -> TableRowSource:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: "postgresql://unused/db"})

    async def _pool(cfg: ProjectionTableConfig) -> Any:
        return pool

    source._pool = _pool  # type: ignore[method-assign]
    return source


@contextmanager
def _client(
    source: Any, topic_map: dict[str, ProjectionTableConfig]
) -> Iterator[TestClient]:
    app.dependency_overrides[get_row_source] = lambda: source
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# AC1: the read is answered from the writer's table, tenant-scoped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_read_returns_the_tables_rows_scoped_to_the_tenant() -> None:
    now = datetime.now(UTC)
    records = [
        {
            "correlation_id": UUID(int=i),
            "tenant_id": UUID(_TENANT),
            "written_at": now - timedelta(minutes=i),
            "cost_usd": Decimal("0.0125"),
            "payload": '{"model": "glm"}',
        }
        for i in range(3)
    ]
    pool = _FakePool(records)
    cfg = _decisions_cfg()
    with _client(_source_over(pool), {_DECISIONS: cfg}) as client:
        resp = client.get(f"/projection/{_DECISIONS}", params={"tenant": _TENANT})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backing"] == "table"
    assert body["row_count"] == 3
    assert body["rows"][0]["written_at"] == records[0]["written_at"].isoformat()
    assert body["rows"][0]["cost_usd"] == "0.0125"
    assert body["rows"][0]["payload"] == {"model": "glm"}
    assert body["latest_event_at"] == records[0]["written_at"].isoformat()
    assert body["tenant"] == _TENANT
    assert body["staleness"]["source"] == "table"

    guc = [s for s in pool.statements if "set_config" in s[0]]
    assert guc
    assert guc[0][1] == ("app.tenant_id", _TENANT)
    select = next(s for s in pool.statements if s[0].startswith("SELECT * FROM"))
    assert '"tenant_id"::text = $1' in select[0]
    assert select[1][0] == _TENANT
    assert pool.readonly
    assert all(pool.readonly)


@pytest.mark.unit
def test_a_tenant_scoped_read_with_no_tenant_is_refused_before_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    get_settings.cache_clear()
    pool = _FakePool()
    try:
        with _client(_source_over(pool), {_DECISIONS: _decisions_cfg()}) as client:
            resp = client.get(f"/projection/{_DECISIONS}")
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 422
    assert resp.json()["error"] == "tenant_context_unresolved"
    assert not [s for s in pool.statements if s[0].startswith("SELECT * FROM")]


@pytest.mark.unit
def test_the_window_is_the_newest_rows_the_cache_retained() -> None:
    cfg = _flow_cfg()
    query = build_window_query(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
    assert '"omninode_internal"."consumer_flow_windows"' in query.sql
    assert (
        f'ORDER BY "projection_cursor" DESC NULLS LAST LIMIT '
        f"{cfg.limit * RETAINED_WINDOW_FACTOR}"
    ) in query.sql
    assert query.sql.endswith('ORDER BY "window_end" DESC NULLS LAST')
    assert query.params == ()


@pytest.mark.unit
def test_a_since_walk_reads_above_the_cursor_with_its_own_type() -> None:
    cfg = _flow_cfg()
    query = build_window_query(
        cfg,
        order_spec=(("projection_cursor", "ASC", None),),
        tenant_id=None,
        since="41",
        since_type="bigint",
    )
    assert '"projection_cursor" > CAST($1::text AS bigint)' in query.sql
    assert 'ORDER BY "projection_cursor" ASC NULLS LAST LIMIT' in query.sql
    assert query.params == ("41",)


@pytest.mark.unit
def test_an_unresolved_relation_refuses_by_name() -> None:
    cfg = _flow_cfg(relation_schema=None)
    with pytest.raises(ProjectionReadError) as raised:
        build_window_query(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
    assert raised.value.code == "projection_relation_unresolved"


@pytest.mark.unit
def test_the_relation_schema_comes_from_the_writer_when_the_contract_records_the_database() -> (
    None
):
    writers = {"work_events": "omninode_internal"}
    assert (
        resolve_relation_schema("omnidash_analytics", "work_events", writers)
        == "omninode_internal"
    )
    assert resolve_relation_schema("public", "delegation_events", writers) == "public"
    assert resolve_relation_schema("omnidash_analytics", "unknown", writers) is None


@pytest.mark.unit
def test_internal_relations_read_through_the_internal_dsn() -> None:
    assert dsn_env_for(_flow_cfg()) == "OMNINODE_INTERNAL_DB_URL"
    assert dsn_env_for(_decisions_cfg()) == DEFAULT_DSN_ENV


@pytest.mark.unit
def test_a_table_the_role_may_not_read_is_a_named_503_not_an_empty_200() -> None:
    pool = _FakePool()
    pool.raise_on_fetch = asyncpg.InsufficientPrivilegeError("permission denied")
    with _client(_source_over(pool), {_FLOW: _flow_cfg()}) as client:
        resp = client.get(f"/projection/{_FLOW}")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "projection_table_unreadable"
    assert "permission denied" not in resp.text


# ---------------------------------------------------------------------------
# AC2: no Kafka on the read path; startup never waits on anything
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_kafka_the_real_lifespan_serves_without_a_broker_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real app, real lifespan, no broker and no DSN configured.

    A Kafka consumer constructed anywhere fails the test. Liveness answers,
    and a read of a served exposure refuses with the unbound DSN named
    instead of hanging or answering an empty 200.
    """
    import aiokafka

    def _no_consumer(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the projection API constructed a Kafka consumer")

    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", _no_consumer)
    for env in ("KAFKA_BOOTSTRAP_SERVERS", DEFAULT_DSN_ENV, "OMNINODE_INTERNAL_DB_URL"):
        monkeypatch.delenv(env, raising=False)
    cfg = _flow_cfg()
    monkeypatch.setattr(api_server, "build_projection_topic_map", lambda: {_FLOW: cfg})

    with TestClient(app) as client:
        health = client.get("/health")
        read = client.get(f"/projection/{_FLOW}")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["served_topics"] == [_FLOW]
    assert read.status_code == 503
    assert read.json()["error"] == "projection_database_unbound"
    assert "OMNINODE_INTERNAL_DB_URL" in read.json()["detail"]
    assert ready.status_code == 503
    assert ready.json()["failures"][_FLOW]["error"] == "projection_database_unbound"


@pytest.mark.unit
def test_ready_names_each_exposure_it_cannot_read() -> None:
    pool = _FakePool()
    pool.raise_on_execute = asyncpg.UndefinedTableError("relation does not exist")
    with _client(
        _source_over(pool), {_FLOW: _flow_cfg(), _DECISIONS: _decisions_cfg()}
    ) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["backing"] == "table"
    assert body["failures"][_FLOW]["error"] == "projection_table_missing"
    assert body["served_topics"] == {_DECISIONS: False, _FLOW: False}


@pytest.mark.unit
def test_ready_when_every_served_table_can_be_read() -> None:
    pool = _FakePool()
    with _client(_source_over(pool), {_FLOW: _flow_cfg()}) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["served_topics"] == {_FLOW: True}


# ---------------------------------------------------------------------------
# Real Postgres: the window, the ordering and the tenant scope, end to end
# ---------------------------------------------------------------------------


async def _connect_or_skip() -> tuple[asyncpg.Connection, str]:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping the real-table read proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn), dsn
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for the real-table read proof: {exc}")


@pytest.mark.integration
async def test_a_real_table_serves_the_newest_window_for_one_tenant_only() -> None:
    connection, dsn = await _connect_or_skip()
    schema = f"omn20152_{uuid4().hex[:12]}"
    other_tenant = str(uuid4())
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(
            f'CREATE TABLE "{schema}".delegation_events ('
            "correlation_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "written_at timestamptz NOT NULL, cost_usd numeric, payload jsonb)"
        )
        base = datetime(2026, 9, 30, tzinfo=UTC)
        for i in range(30):
            await connection.execute(
                f'INSERT INTO "{schema}".delegation_events VALUES ($1, $2, $3, $4, $5)',
                uuid4(),
                UUID(_TENANT if i % 3 else other_tenant),
                base + timedelta(minutes=i),
                Decimal(i),
                f'{{"i": {i}}}',
            )
        cfg = _decisions_cfg(relation_schema=schema, limit=5)
        source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
        try:
            rows = await source.rows(
                cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT
            )
            latest = await source.latest_event_at(
                cfg, tenant_id=_TENANT, window_rows=rows
            )
        finally:
            await source.close()
        assert len(rows) == 5 * RETAINED_WINDOW_FACTOR
        assert {row["tenant_id"] for row in rows} == {_TENANT}
        stamps = [row["written_at"] for row in rows]
        assert stamps == sorted(stamps, reverse=True)
        assert latest == base + timedelta(minutes=29)
        assert rows[0]["payload"] == {"i": 29}
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
