# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977: prove the writer's real SQL with its migration in Postgres."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
)

_MIGRATION_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_metering_summary"
    / "migrations"
)
# The table and every later column migration; 0001 is grants only and needs a
# role this disposable schema does not create.
_MIGRATIONS = tuple(
    _MIGRATION_DIR / name
    for name in (
        "0000_create_metering_summary.sql",
        "0002_metering_summary_savings_per_measured_run.sql",
    )
)
_SCHEMA = "omn19977_metering_summary_write_path_test"


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
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for metering-summary proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


class _ScopedConnectionAdapter:
    """Run the writer's statements on a real connection in a disposable schema."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(_scoped(query), *params)
        return [dict(row) for row in rows]


def _scoped_writer(conn: asyncpg.Connection) -> MeteringSummaryProjectionWriter:
    """The real writer, its statements run on ``conn`` in the disposable schema."""
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = _ScopedConnectionAdapter(conn)  # type: ignore[assignment]
    writer._standalone_bindings = None
    return writer


@pytest.mark.integration
async def test_replacement_and_replay_against_real_postgres() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        writer = _scoped_writer(conn)
        first = ModelMeteringSummaryFoldRequest(
            tenant_id="local",
            baseline_model="unresolved-model",
            as_of=datetime(2026, 9, 28, 12, tzinfo=UTC),
        )
        query = f"SELECT * FROM {_SCHEMA}.metering_summary"
        assert (await writer._project(first))["rows_upserted"] == 1
        initial = await conn.fetch(query)
        assert len(initial) == 1
        assert initial[0]["as_of"] == first.as_of.isoformat()
        assert initial[0]["savings_usd"] is None

        second = first.model_copy(update={"as_of": first.as_of + timedelta(hours=1)})
        assert (await writer._project(second))["rows_upserted"] == 1
        replaced = await conn.fetch(query)
        assert len(replaced) == 1
        assert replaced[0]["as_of"] == second.as_of.isoformat()
        assert replaced[0]["savings_usd"] is None
        expected = HandlerProjectionMeteringSummary().handle(second).rows[0]
        assert dict(replaced[0]) == expected.model_dump(mode="json")

        assert (await writer._project(second))["rows_upserted"] == 1
        replayed = await conn.fetch(query)
        assert len(replayed) == 1
        assert dict(replayed[0]) == dict(replaced[0])
        assert replayed[0]["summary_json"].encode("utf-8") == replaced[0][
            "summary_json"
        ].encode("utf-8")
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


@pytest.mark.integration
async def test_omn20009_the_writer_stores_the_saving_per_measured_run() -> None:
    """OMN-20009 G9: the served column is written by the real upsert."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        writer = _scoped_writer(conn)
        as_of = datetime(2026, 9, 28, 12, tzinfo=UTC)
        baseline = ModelCounterfactualBaseline(
            model="baseline-model",
            price_in_per_1k=Decimal("1"),
            price_out_per_1k=Decimal("2"),
            as_of="2026-09-01",
            pricing_manifest_version="1",
            source="pricing_manifest",
        )
        request = ModelMeteringSummaryFoldRequest(
            tenant_id="local",
            baseline_model="baseline-model",
            baseline=baseline,
            as_of=as_of,
            records=tuple(
                ModelMeteringRecord(
                    correlation_id=f"run-{index}",
                    occurred_at=as_of - timedelta(hours=1, minutes=index),
                    model="local-a",
                    tokens_in=1000,
                    tokens_out=0,
                    spend_usd=Decimal(spend),
                )
                for index, spend in enumerate(("0", "0", "0.1"))
            ),
        )
        await writer._project(request)
        rows = await conn.fetch(
            f"SELECT window_kind, savings_usd, savings_per_measured_run_usd "
            f"FROM {_SCHEMA}.metering_summary WHERE window_kind = 'all'"
        )
        assert len(rows) == 1
        assert Decimal(rows[0]["savings_usd"]) == Decimal("2.9")
        assert rows[0]["savings_per_measured_run_usd"] == "0.966667"
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()
