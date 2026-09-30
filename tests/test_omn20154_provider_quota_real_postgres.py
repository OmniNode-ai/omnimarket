# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: the provider quota merge and read, against real Postgres.

Applies the node migrations to a unique scratch schema, drives the writer's
actual upsert through the pure fold, and reads the result back with the
reader's actual SQL. Everything runs in one transaction that is rolled back.
Without INTEGRATION_POSTGRES credentials this proof skips.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.inference.provider_quota_state import active_blocks_sql
from omnimarket.nodes.node_projection_provider_quota.handlers import (
    HandlerProjectionProviderQuota,
)
from omnimarket.nodes.node_projection_provider_quota.handlers.handler_provider_quota_writer import (
    _UPSERT_QUOTA_ROW,
)
from omnimarket.nodes.node_projection_provider_quota.models import (
    ModelProviderQuotaProjectionRequest,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_provider_quota/migrations"
)
_T0 = datetime(2026, 9, 30, 13, 42, tzinfo=UTC)
_TENANT = UUID("11111111-1111-1111-1111-111111111111")


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if not dsn:
        secret = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        if not secret:
            pytest.skip(
                "INTEGRATION_POSTGRES_DSN/INTEGRATION_POSTGRES_PASSWORD unset -- "
                "skipping the provider-quota write-path proof"
            )
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn, timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for the write-path proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    """Retarget objects and grant recipients without requiring cluster roles."""
    return (
        statement.replace("public.", f"{schema}.")
        .replace("ON SCHEMA public", f"ON SCHEMA {schema}")
        .replace("TO tenant_projection_writer", "TO CURRENT_USER")
        .replace("rolname = 'tenant_projection_writer'", "rolname = CURRENT_USER")
        .replace("'tenant_projection_writer',", "CURRENT_USER,")
        .replace("TO app_dashboard", "TO CURRENT_USER")
    )


def _event(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "tenant_id": str(_TENANT),
        "credential_ref": "llm.glm.api_key",
        "provider_id": "zai",
        "model_name": "glm-5.3",
        "outcome": "call_ok",
        "http_status": 200,
        "call_started_at": _T0 - timedelta(seconds=1),
        "observed_at": _T0,
        "source": "runtime_orchestrator",
    } | updates


def _hit(at: datetime, **updates: Any) -> dict[str, Any]:
    return (
        _event(
            outcome="limit_hit",
            http_status=429,
            provider_code="1302",
            disposition="cooldown",
            block_scope="provider",
            blocked_until=at + timedelta(seconds=60),
            reason="zai code 1302: capacity refusal",
            call_started_at=at - timedelta(seconds=1),
            observed_at=at,
        )
        | updates
    )


async def _project(conn: asyncpg.Connection, schema: str, event: dict[str, Any]) -> int:
    """Run the fold and the writer's own statement; return rows written."""
    written = 0
    rows = (
        HandlerProjectionProviderQuota()
        .handle(ModelProviderQuotaProjectionRequest.model_validate(event))
        .rows
    )
    for row in rows:
        returned = await conn.fetch(
            _scoped(_UPSERT_QUOTA_ROW, schema),
            row.tenant_id,
            row.credential_ref,
            row.provider_id,
            row.model_scope,
            row.observed_at,
            row.window_seconds,
            row.outcome.value,
            row.http_status,
            row.provider_code,
            row.counts_hit,
            row.sets_block,
            row.disposition,
            row.blocked_until,
            row.blocked_indefinitely,
            row.block_reason,
            row.clears_blocks_before,
        )
        written += len(returned)
    return written


async def _active(
    conn: asyncpg.Connection, schema: str, as_of: datetime
) -> list[asyncpg.Record]:
    sql = (
        active_blocks_sql(f"{schema}.provider_quota_state")
        .replace("%(tenant_id)s", "$1")
        .replace("%(as_of)s", "$2")
    )
    return list(await conn.fetch(sql, str(_TENANT), as_of))


async def _row(
    conn: asyncpg.Connection, schema: str, model_scope: str
) -> asyncpg.Record:
    record = await conn.fetchrow(
        f"SELECT * FROM {schema}.provider_quota_state WHERE model_scope = $1",
        model_scope,
    )
    assert record is not None
    return record


async def _with_scratch_schema(body: Any) -> None:
    conn = await _connect_or_skip()
    schema = f"omn20154_provider_quota_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await conn.execute(f"CREATE SCHEMA {schema}")
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))
        await body(conn, schema)
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_a_limit_hit_survives_as_a_durable_block() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        assert await _project(conn, schema, _hit(_T0)) == 2
        active = await _active(conn, schema, _T0 + timedelta(seconds=30))
        assert [(r["provider_id"], r["model_scope"]) for r in active] == [("zai", "*")]
        assert active[0]["disposition"] == "cooldown"
        assert active[0]["last_provider_code"] == "1302"
        # The block lifts on its own at the provider's time; nothing sweeps it.
        assert await _active(conn, schema, _T0 + timedelta(seconds=61)) == []

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_calls_are_counted_per_window_on_both_rows() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        for second in (0, 10, 20):
            at = _T0 + timedelta(seconds=second)
            await _project(conn, schema, _event(observed_at=at, call_started_at=at))
        await _project(conn, schema, _hit(_T0 + timedelta(seconds=30)))
        # A call 90s in opens a new 60s window.
        later = _T0 + timedelta(seconds=90)
        await _project(
            conn,
            schema,
            _event(
                model_name="glm-4.7-flash", observed_at=later, call_started_at=later
            ),
        )
        provider = await _row(conn, schema, "*")
        assert provider["calls_total"] == 5
        assert provider["hits_total"] == 1
        assert provider["window_calls"] == 1
        assert provider["window_started_at"] == later
        model = await _row(conn, schema, "glm-5.3")
        assert model["calls_total"] == 4
        assert model["window_calls"] == 4
        assert model["last_http_status"] == 429

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_only_a_call_sent_after_the_refusal_clears_it() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        hit_at = _T0
        await _project(conn, schema, _hit(hit_at))
        # In flight when the provider refused: completes later, clears nothing.
        await _project(
            conn,
            schema,
            _event(
                call_started_at=hit_at - timedelta(seconds=5),
                observed_at=hit_at + timedelta(seconds=2),
            ),
        )
        assert len(await _active(conn, schema, hit_at + timedelta(seconds=3))) == 1
        # Sent after the refusal and answered: the key is usable again.
        await _project(
            conn,
            schema,
            _event(
                call_started_at=hit_at + timedelta(seconds=4),
                observed_at=hit_at + timedelta(seconds=5),
            ),
        )
        assert await _active(conn, schema, hit_at + timedelta(seconds=6)) == []

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_replay_and_out_of_order_observations_change_nothing() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        event = _hit(_T0)
        assert await _project(conn, schema, event) == 2
        assert await _project(conn, schema, event) == 0
        assert (
            await _project(conn, schema, _event(observed_at=_T0 - timedelta(seconds=1)))
            == 0
        )
        provider = await _row(conn, schema, "*")
        assert provider["calls_total"] == 1

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_a_later_hit_never_shortens_a_block() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        long_until = _T0 + timedelta(hours=1)
        await _project(
            conn,
            schema,
            _hit(
                _T0,
                provider_code="1316",
                disposition="disable_until_reset",
                blocked_until=long_until,
            ),
        )
        await _project(conn, schema, _hit(_T0 + timedelta(seconds=5)))
        provider = await _row(conn, schema, "*")
        assert provider["blocked_until"] == long_until

    await _with_scratch_schema(body)
