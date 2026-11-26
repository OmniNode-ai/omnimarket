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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import _refuse_unbound_tenant_scoped_write
from omnimarket.handlers.handler_savings_priced_models_fill import (
    refresh_shared_priced_models,
)
from omnimarket.models.model_local_install_schema import local_priced_models_rows
from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import metering_record_from_row
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
    store_refresh_command_topic,
)
from omnimarket.nodes.node_projection_metering_summary.metering_refresh_coalescer import (
    MeteringRefreshCoalescer,
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
);
-- OMN-26444: the refresh refills the manifest's priced models first, as
-- node_projection_savings migration 102 creates them.
CREATE TABLE {_SCHEMA}.savings_priced_models (
    model_key TEXT PRIMARY KEY,
    free BOOLEAN NOT NULL
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


@pytest.mark.integration
async def test_a_burst_of_terminal_refreshes_folds_once_and_the_held_back_one_lands_in_the_next_run() -> (
    None
):
    """OMN-24563: a burst of terminal refreshes folds once; the held-back run lands in the next run."""
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
            "cost_usd, cost_savings_usd, data_source) "
            "VALUES ('a', $1::uuid, $2, 'qwen', 'document', 100, 50, 0.0001, 0.01, 'real')",
            _TENANT,
            datetime(2026, 10, 19, 9, tzinfo=UTC),
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _ScopedConnectionAdapter(conn))
        writer._standalone_bindings = None
        writer._coalescer = MeteringRefreshCoalescer(timedelta(seconds=300))
        clock = [datetime(2026, 10, 19, 10, 0, tzinfo=UTC)]
        writer._clock = lambda: clock[0]

        def _request(refresh_id: str) -> ModelMeteringSummaryRefreshRequested:
            return ModelMeteringSummaryRefreshRequested(
                refresh_id=refresh_id,
                reason=EnumMeteringRefreshReason.DELEGATION_TERMINAL,
                tenant_ids=(_TENANT,),
                days=frozenset({date(2026, 10, 19)}),
                requested_at=clock[0],
            )

        first = await writer._refresh(_request("burst-first"))
        assert first["refresh_coalesced"] is False
        row = await conn.fetchrow(
            f"SELECT runs_total, tokens_in FROM {_SCHEMA}.metering_summary "
            "WHERE window_kind = 'all'"
        )
        assert row is not None
        assert row["runs_total"] == 1

        await conn.execute(
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, "
            "created_at, delegated_to, task_type, tokens_input, tokens_output, "
            "cost_usd, cost_savings_usd, data_source) "
            "VALUES ('b', $1::uuid, $2, 'qwen', 'document', 100, 50, 0.0001, 0.01, 'real')",
            _TENANT,
            datetime(2026, 10, 19, 10, 0, 30, tzinfo=UTC),
        )
        clock[0] = clock[0] + timedelta(seconds=30)
        second = await writer._refresh(_request("burst-held"))
        assert second["refresh_coalesced"] is True
        assert second["refresh_coalesced_total"] == 1
        assert second["refresh_pending_tenants"] == [_TENANT]
        row = await conn.fetchrow(
            f"SELECT runs_total FROM {_SCHEMA}.metering_summary "
            "WHERE window_kind = 'all'"
        )
        assert row is not None
        assert row["runs_total"] == 1

        clock[0] = clock[0] + timedelta(seconds=400)
        third = await writer._refresh(_request("burst-lands"))
        assert third["refresh_coalesced"] is False
        assert third["refresh_pending_tenants"] == []
        row = await conn.fetchrow(
            f"SELECT runs_total, tokens_in FROM {_SCHEMA}.metering_summary "
            "WHERE window_kind = 'all'"
        )
        assert row is not None
        assert row["runs_total"] == 2
        assert row["tokens_in"] == 200
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


@pytest.mark.integration
async def test_the_refresh_fills_the_priced_models_and_drops_a_stale_one() -> None:
    """OMN-26444 (FILL-26444): on a real Postgres the writer's refresh replaces savings_priced_models with the pricing manifest's models before it folds, and a model the manifest no longer names is removed."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.savings_priced_models (model_key, free) VALUES ('model-the-manifest-no-longer-names', FALSE)"
        )
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, created_at, delegated_to, task_type, tokens_input, tokens_output, cost_usd, cost_savings_usd, data_source) "
            "VALUES ('a', $1::uuid, $2, 'qwen', 'document', 100, 50, 0.0001, 0.01, 'real')",
            _TENANT,
            datetime(2026, 10, 19, 9, tzinfo=UTC),
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _ScopedConnectionAdapter(conn))
        writer._standalone_bindings = None
        result = await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="priced-models-fill-test",
                reason=EnumMeteringRefreshReason.BACKFILL,
                requested_at=datetime(2026, 10, 19, 10, tzinfo=UTC),
            )
        )
        assert result["tenants_refreshed"] == [_TENANT]
        rows = await conn.fetch(
            f"SELECT model_key, free FROM {_SCHEMA}.savings_priced_models ORDER BY model_key"
        )
        expected = sorted((key, bool(free)) for key, free in local_priced_models_rows())
        assert expected, "positive control: the manifest names at least one model"
        assert [(r["model_key"], r["free"]) for r in rows] == expected
        assert "model-the-manifest-no-longer-names" not in {
            r["model_key"] for r in rows
        }
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


@pytest.mark.integration
async def test_an_incremental_refresh_continues_from_the_watermark_and_equals_a_rebuild() -> (
    None
):
    """OMN-21288: the continuation SQL (watermark read, stored-day read) on real Postgres.

    A refresh after one new run reads and folds that run only, through the
    typed ``created_at >= $2`` and ``window_start = ANY($3::text[])`` statements,
    and leaves the rows a rebuild on demand writes.
    """
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        insert = (
            f"INSERT INTO {_SCHEMA}.delegation_events (correlation_id, tenant_id, "
            "created_at, delegated_to, task_type, tokens_input, tokens_output, "
            "cost_usd, cost_savings_usd) "
            "VALUES ($1, $2::uuid, $3, 'qwen', 'document', 100, 50, 0.0001, 0.01)"
        )
        await conn.executemany(
            insert,
            [
                ("a", _TENANT, datetime(2026, 10, 18, 9, tzinfo=UTC)),
                ("b", _TENANT, datetime(2026, 10, 19, 9, tzinfo=UTC)),
            ],
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._db = cast(Any, _ScopedConnectionAdapter(conn))
        writer._standalone_bindings = None

        first = await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="bootstrap",
                reason=EnumMeteringRefreshReason.SCHEDULE,
                requested_at=datetime(2026, 10, 19, 10, tzinfo=UTC),
            )
        )
        assert first["tenants_rebuilt_in_full"] == [_TENANT]

        await conn.execute(insert, "c", _TENANT, datetime(2026, 10, 19, 11, tzinfo=UTC))
        second = await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="terminal-c",
                reason=EnumMeteringRefreshReason.DELEGATION_TERMINAL,
                tenant_ids=(_TENANT,),
                days=frozenset({date(2026, 10, 19)}),
                requested_at=datetime(2026, 10, 19, 11, tzinfo=UTC),
            )
        )
        assert second["tenants_rebuilt_in_full"] == []
        assert second["records_folded"] == 1

        select = (
            f"SELECT * FROM {_SCHEMA}.metering_summary "
            "ORDER BY window_kind, window_start"
        )

        def comparable(rows: list[Any]) -> list[dict[str, Any]]:
            # A day row's as_of is when that day was last folded; a rebuild
            # folds every day, an incremental refresh only the days it touched.
            return [
                {
                    k: v
                    for k, v in dict(r).items()
                    if not (r["window_kind"] == "day" and k == "as_of")
                }
                for r in rows
            ]

        incremental = comparable(await conn.fetch(select))
        assert [r["runs_total"] for r in incremental] == [3, 1, 2]
        assert incremental[0]["as_of"] == "2026-10-19T11:00:00.000001+00:00"

        await writer._refresh(
            ModelMeteringSummaryRefreshRequested(
                refresh_id="rebuild",
                reason=EnumMeteringRefreshReason.BACKFILL,
                requested_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
            )
        )
        assert comparable(await conn.fetch(select)) == incremental
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


class _DeclaredTablesOnlyBindings:
    """Standalone bindings that, like the runtime's, refuse a table the contract does not declare."""

    def __init__(self, declared: set[str]) -> None:
        self._declared = declared

    def read_binding_for(self, table: str) -> str:
        return self.write_binding_for(table)

    def write_binding_for(self, table: str) -> str:
        if table not in self._declared:
            raise KeyError(table)
        return "tenant_projection"


@pytest.mark.integration
async def test_the_fill_runs_on_a_declared_table_binding_in_standalone_mode() -> None:
    """OMN-26444: savings_priced_models is not a db_io declaration of the metering contract, so in standalone mode the fill must resolve a binding through a declared table, not the undeclared name, and still replace the rows on a real Postgres."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        await conn.execute(_DELEGATION_EVENTS)
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.savings_priced_models (model_key, free) VALUES ('stale-model', FALSE)"
        )
        writer = MeteringSummaryProjectionWriter.__new__(
            MeteringSummaryProjectionWriter
        )
        writer._standalone_bindings = _DeclaredTablesOnlyBindings(
            {"metering_summary", "delegation_events"}
        )
        writer._db_by_binding = {
            "tenant_projection": cast(Any, _ScopedConnectionAdapter(conn))
        }
        await refresh_shared_priced_models(writer.db_for("metering_summary"))
        rows = await conn.fetch(
            f"SELECT model_key FROM {_SCHEMA}.savings_priced_models"
        )
        assert "stale-model" not in {r["model_key"] for r in rows}
        assert {r["model_key"] for r in rows} == {
            key for key, _ in local_priced_models_rows()
        }
        with pytest.raises(KeyError):
            writer.db_for("savings_priced_models")
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()
