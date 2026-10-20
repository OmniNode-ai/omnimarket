# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the metering store refresh's real SQL against real Postgres.

The refresh reads delegation_events as Postgres types them (UUID tenant,
TIMESTAMPTZ time, NUMERIC money) and writes metering_summary through the
writer's own upsert. A mock store cannot catch a column-type mismatch, so this
runs every statement on a real connection in a disposable schema.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)

_MIGRATION_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_metering_summary"
    / "migrations"
)
_MIGRATIONS = tuple(
    _MIGRATION_DIR / name
    for name in (
        "0000_create_metering_summary.sql",
        "0002_metering_summary_savings_per_measured_run.sql",
        "0003_metering_summary_compression_and_cache_hit.sql",
        "0004_metering_summary_savings_pct.sql",
    )
)
_SCHEMA = "omn17427_metering_store_refresh_test"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
# The delegation_events columns the refresh reads, typed as the delegation
# projection's migrations type them.
_DELEGATION_EVENTS = f"""
CREATE TABLE {_SCHEMA}.delegation_events (
    correlation_id TEXT NOT NULL,
    tenant_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delegated_to TEXT NOT NULL DEFAULT '',
    model_name TEXT DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    tokens_input INT NOT NULL DEFAULT 0,
    tokens_output INT NOT NULL DEFAULT 0,
    cost_usd NUMERIC,
    cost_savings_usd NUMERIC NOT NULL DEFAULT 0,
    data_source TEXT NOT NULL DEFAULT 'real'
)
"""


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD", "")
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- skipping DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for metering store refresh proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


class _ScopedConnectionAdapter:
    """The writer's adapter surface, run on a real connection in the schema."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(_scoped(query), *params)
        return [dict(row) for row in rows]


@pytest.mark.integration
async def test_store_refresh_reads_typed_runs_and_writes_rows() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, "
            "created_at, delegated_to, task_type, tokens_input, tokens_output, "
            "cost_usd, cost_savings_usd, data_source) "
            "VALUES ($1, $2::uuid, $3, 'qwen', 'document', $4, $5, 0.0001, 0.01, $6)",
            [
                ("a", _TENANT, datetime(2026, 10, 18, 9, tzinfo=UTC), 100, 50, "real"),
                ("b", _TENANT, datetime(2026, 10, 19, 9, tzinfo=UTC), 100, 50, "real"),
                (
                    "seed",
                    _TENANT,
                    datetime(2026, 10, 19, 9, tzinfo=UTC),
                    9,
                    9,
                    "fixture",
                ),
            ],
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _ScopedConnectionAdapter(conn))
        writer._standalone_bindings = None

        result = await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="backfill-test",
                reason=EnumMeteringRefreshReason.BACKFILL,
                requested_at=datetime(2026, 10, 19, 10, tzinfo=UTC),
            )
        )

        assert result["tenants_refreshed"] == [_TENANT]
        rows = await conn.fetch(
            f"SELECT window_kind, window_start, runs_total, tokens_in, as_of "
            f"FROM {_SCHEMA}.metering_summary ORDER BY window_kind, window_start"
        )
        assert [(r["window_kind"], r["window_start"]) for r in rows] == [
            ("all", ""),
            ("day", "2026-10-18"),
            ("day", "2026-10-19"),
        ]
        # The fixture row is never in a stored figure.
        assert rows[0]["runs_total"] == 2
        assert rows[0]["tokens_in"] == 200
        assert rows[0]["as_of"] == "2026-10-19T09:00:00.000001+00:00"
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()
