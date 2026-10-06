# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972 against a real PostgreSQL: a slug is served as its registry UUID.

The unit suite drives the serving path over a fake pool. What only a real
database proves: the registry lookup the reader issues is the writer's own
(unqualified ``tenant_registry_mirror``, keyed by ``tenant_slug``), it returns a
``uuid`` the window then binds against a ``uuid`` tenant column through the
``::text`` comparison, and two tenants' rows in one table stay apart.

Runs where CI provides ``INTEGRATION_POSTGRES_*`` / ``POSTGRES_PASSWORD`` and
skips otherwise. It never starts a database of its own.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import DEFAULT_DSN_ENV, TableRowSource

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_COLUMNS = ("correlation_id", "tenant_id", "written_at")


async def _connect_or_skip() -> tuple[asyncpg.Connection, str]:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping the real registry read")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn), dsn
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the real registry read: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc


def _cfg(schema: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name=schema,
        relation_schema=schema,
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        limit=50,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


@pytest.mark.integration
async def test_a_real_registry_serves_a_slug_as_its_uuid_and_refuses_an_unknown_one() -> (
    None
):
    connection, dsn = await _connect_or_skip()
    schema = f"omn19972_{uuid4().hex[:12]}"
    slug, mine, other = "local-lane", uuid4(), uuid4()
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        # The mirror's own shape (node_projection_tenant_registry 0000).
        await connection.execute(
            f'CREATE TABLE "{schema}".tenant_registry_mirror ('
            "tenant_slug text PRIMARY KEY, tenant_uuid uuid NOT NULL, "
            "status text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now())"
        )
        await connection.execute(
            f'INSERT INTO "{schema}".tenant_registry_mirror '
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
            slug,
            mine,
        )
        await connection.execute(
            f'CREATE TABLE "{schema}".delegation_events ('
            "correlation_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "written_at timestamptz NOT NULL)"
        )
        base = datetime(2026, 10, 5, tzinfo=UTC)
        for i in range(6):
            await connection.execute(
                f'INSERT INTO "{schema}".delegation_events VALUES ($1, $2, $3)',
                uuid4(),
                mine if i % 2 == 0 else other,
                base + timedelta(minutes=i),
            )
        # The writer's lookup is unqualified, so the reader's is too: the
        # search_path is what places it in this disposable schema.
        source = TableRowSource(
            environ={DEFAULT_DSN_ENV: f"{dsn}?search_path={schema}"}
        )
        topic_map = {_DECISIONS: _cfg(schema)}
        try:
            served = await read_projection_page(
                _DECISIONS, topic_map=topic_map, source=source, tenant=slug
            )
            refused = await read_projection_page(
                _DECISIONS, topic_map=topic_map, source=source, tenant="never-minted"
            )
        finally:
            await source.close()

        body: dict[str, Any] = served.body
        assert served.status_code == 200, body
        assert body["tenant"] == str(mine)
        assert body["row_count"] == 3
        assert {UUID(row["tenant_id"]) for row in body["rows"]} == {mine}
        assert refused.status_code == 422, refused.body
        assert refused.body["error"] == "tenant_context_unresolved"
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
