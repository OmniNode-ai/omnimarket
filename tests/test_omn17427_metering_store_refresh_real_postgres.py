# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the metering store refresh's real SQL against real Postgres.

The refresh reads delegation_events as Postgres types them (UUID tenant,
TIMESTAMPTZ time, NUMERIC money) and writes metering_summary through the
writer's own upsert. A mock store cannot catch a column-type mismatch, so this
runs every statement on a real connection in a disposable schema.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import _refuse_unbound_tenant_scoped_write
from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import metering_record_from_row
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
    store_refresh_command_topic,
)
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary_topic,
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
    data_source TEXT NOT NULL DEFAULT 'real',
    terminal_failure_cause TEXT
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
        # The real adapter's refusal of an unbound tenant-scoped write (OMN-19513).
        _refuse_unbound_tenant_scoped_write(query, tenant)
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


@pytest.mark.integration
async def test_a_run_returned_to_its_caller_is_stored_as_zero_savings() -> None:
    """OMN-21264: the stored summary the dashboard reads books a returned run at zero.

    No answer was accepted, so the run saved nothing. Stored as measured, never
    as unknown, whatever its recorded tokens.
    """
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, "
            "created_at, delegated_to, task_type, tokens_input, tokens_output, "
            "cost_usd, terminal_failure_cause) "
            "VALUES ('returned', $1::uuid, $2, 'gpt-5-nano', 'document', 0, 0, "
            "0.0006, 'returned_to_caller')",
            _TENANT,
            datetime(2026, 10, 19, 9, tzinfo=UTC),
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _ScopedConnectionAdapter(conn))
        writer._standalone_bindings = None

        await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="returned-to-caller-test",
                reason=EnumMeteringRefreshReason.BACKFILL,
                requested_at=datetime(2026, 10, 19, 10, tzinfo=UTC),
            )
        )

        row = await conn.fetchrow(
            f"SELECT runs_total, runs_measured, runs_unknown_tokens, savings_usd, "
            f"baseline_state FROM {_SCHEMA}.metering_summary "
            "WHERE window_kind = 'all'"
        )
        assert row is not None
        assert row["runs_total"] == 1
        assert row["runs_measured"] == 1
        assert row["runs_unknown_tokens"] == 0
        if row["baseline_state"] == "resolved":
            assert Decimal(row["savings_usd"]) == 0
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


class _DispatchConnectionAdapter(_ScopedConnectionAdapter):
    """Open the real connection on the synchronous dispatch's event loop."""

    async def connect(self) -> None:
        self._conn = await _connect_or_skip()

    async def close(self) -> None:
        await self._conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("command", ["refresh", "fold"])
async def test_runtime_envelope_context_persists_summary(command: str) -> None:
    """Both public commands strip dispatch context before writing real rows."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        occurred_at = datetime(2026, 10, 19, 9, tzinfo=UTC)
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, "
            "created_at, delegated_to, task_type, tokens_input, tokens_output, "
            "cost_usd, cost_savings_usd) "
            "VALUES ('envelope-run', $1::uuid, $2, 'qwen', 'document', "
            "100, 50, 0.0001, 0.01)",
            _TENANT,
            occurred_at,
        )
        payload: dict[str, Any]
        if command == "refresh":
            payload = ModelMeteringSummaryRefreshRequested(
                refresh_id="envelope-refresh",
                reason=EnumMeteringRefreshReason.BACKFILL,
                requested_at=occurred_at + timedelta(seconds=1),
            ).model_dump(mode="json")
            topic = store_refresh_command_topic()
        else:
            stored = await conn.fetchrow(f"SELECT * FROM {_SCHEMA}.delegation_events")
            assert stored is not None
            record = metering_record_from_row(dict(stored))
            assert record is not None
            payload = {
                "tenant_id": _TENANT,
                "records": [record.model_dump(mode="json")],
                "baseline_model": "claude-sonnet-4-5",
                "as_of": (occurred_at + timedelta(microseconds=1)).isoformat(),
            }
            topic = refresh_metering_summary_topic()
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _DispatchConnectionAdapter(conn))
        writer._standalone_bindings = None
        result = await asyncio.to_thread(
            writer.handle,
            {
                **payload,
                "_topic": topic,
                "_db": object(),
                "_event_type": "metering-summary-refresh-requested",
                "_envelope_id": "7d809fe8-7d59-53fb-b52a-b70762383c15",
                "_envelope_timestamp": "2026-10-21T16:30:00.017211Z",
                "_partition": 0,
                "_offset": 41,
                "_future_dispatch_context": object(),
            },
        )
        if command == "refresh":
            assert result["tenants_refreshed"] == [_TENANT]
        else:
            assert result["rows_upserted"] == 2
        rows = await conn.fetch(
            f"SELECT tenant_id, window_kind, window_start, runs_total, "
            f"tokens_in, tokens_out, as_of FROM {_SCHEMA}.metering_summary "
            "ORDER BY window_kind, window_start"
        )
        assert [(row["window_kind"], row["window_start"]) for row in rows] == [
            ("all", ""),
            ("day", "2026-10-19"),
        ]
        for row in rows:
            assert str(row["tenant_id"]) == _TENANT
            assert row["runs_total"] == 1
            assert row["tokens_in"] == 100
            assert row["tokens_out"] == 50
            assert row["as_of"] == "2026-10-19T09:00:00.000001+00:00"
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()
