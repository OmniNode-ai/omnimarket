# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23169: a synthetic run is left out of a user's totals and stays on Runs.

Migration 0064 stores ``data_source = 'experiment'`` for a terminal whose
provenance traffic class is synthetic. The metering readers and the delegation
summary view excluded only ``fixture`` rows, so every synthetic run raised the
run, token, spend and savings totals. Each store is driven with one ordinary
run and one synthetic run: the totals must equal the ordinary run alone, and
the synthetic run must still be listed.

SQLite runs everywhere. Postgres applies the node's whole migration chain to a
throwaway schema (the INTEGRATION_POSTGRES_* variables, as the OMN-19448 module)
and skips when none is reachable.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_metering_summary_compute import (
    ModelCounterfactualBaseline,
    metering_record_from_row,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    RECORDS_SQL,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import read_metering_records
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    PROJECTION_TENANT_UUID,
    seed_tenant_registry,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _base_dsn,
    _provisioned_runner,
)
from tests.unit.projection.test_delegation_data_source_experiment_omn19448 import (
    recorded_terminal,
)

_REAL_SAVINGS = 0.25
_SYNTHETIC_SAVINGS = 7.5
_BASELINE = ModelCounterfactualBaseline(
    model="test-baseline",
    price_in_per_1k=Decimal("1"),
    price_out_per_1k=Decimal("2"),
    as_of="2026-09-28",
    pricing_manifest_version="1",
    source="test_manifest",
)


def _terminal(traffic_class: str | None, savings: float) -> dict[str, Any]:
    payload = recorded_terminal(traffic_class)
    payload["metrics"] = {
        **payload.get("metrics", {}),
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cost_usd": 0.01,
        "cost_savings_usd": savings,
    }
    return payload


def _write(db: Any, payload: dict[str, Any]) -> None:
    HandlerProjectionDelegation().handle({**payload, "_db": db})


@pytest.mark.unit
def test_sqlite_totals_ignore_a_synthetic_run_and_runs_still_lists_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(path)
    seed_tenant_registry(db)
    real = _terminal(None, _REAL_SAVINGS)
    _write(db, real)
    monkeypatch.setattr(
        sqlite_metering_summary, "resolve_baseline", lambda _: _BASELINE
    )
    tenant = str(PROJECTION_TENANT_UUID)
    now = datetime.now(UTC) + timedelta(minutes=1)

    def totals() -> tuple[dict[str, Any], dict[str, Any]]:
        sqlite_metering_summary.refresh_metering_summary(
            path, tenant, _BASELINE.model, now
        )
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            metering = dict(
                conn.execute(
                    "SELECT runs_total, runs_measured, tokens_in, tokens_out, "
                    "spend_usd, savings_usd FROM metering_summary "
                    "WHERE window_kind = 'all'"
                ).fetchone()
            )
            summary = dict(
                conn.execute(
                    'SELECT "totalDelegations", "totalSavingsUsd", '
                    '"qualityGateTotal", "byTaskType", "byModel" '
                    "FROM projection_delegation_summary"
                ).fetchone()
            )
        finally:
            conn.close()
        return metering, summary

    before = totals()
    assert before[0]["runs_total"] == 1
    assert before[1]["totalDelegations"] == 1

    synthetic = _terminal("synthetic", _SYNTHETIC_SAVINGS)
    _write(db, synthetic)
    # Positive control: the synthetic run is stored, typed experiment.
    stored = {
        r["correlation_id"]: r["data_source"] for r in db.query("delegation_events")
    }
    assert stored == {
        real["correlation_id"]: "real",
        synthetic["correlation_id"]: "experiment",
    }

    assert totals() == before
    assert [r.correlation_id for r in read_metering_records(db_path=path)] == [
        real["correlation_id"]
    ]
    # Fixtures can be asked for back; an experiment run cannot.
    assert [
        r.correlation_id
        for r in read_metering_records(db_path=path, include_fixtures=True)
    ] == [real["correlation_id"]]

    topics = build_projection_topic_map()
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(
                topic_map=topics, row_source=SqliteTableRowSource(path)
            ),
            topic_map=topics,
            tenant=tenant,
        )
    )
    response = client.get(
        "/projection/onex.snapshot.projection.delegation.decisions.v1"
    )
    assert response.status_code == 200, response.json()
    listed = {r["correlation_id"]: r["data_source"] for r in response.json()["rows"]}
    assert listed == stored


@pytest.mark.unit
def test_a_sqlite_store_opened_before_the_step_takes_the_revised_view(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(path)
    seed_tenant_registry(db)
    _write(db, _terminal(None, _REAL_SAVINGS))
    _write(db, _terminal("synthetic", _SYNTHETIC_SAVINGS))
    conn = sqlite3.connect(path)
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn23169_delegation_summary_excludes_experiment'"
    )
    # The 0050-equivalent view that counted every non-fixture row.
    conn.execute("DROP VIEW projection_delegation_summary")
    conn.execute(
        'CREATE VIEW projection_delegation_summary AS SELECT 2 AS "totalDelegations"'
    )
    conn.commit()
    conn.close()

    SqliteDatabaseAdapter(path)._connect().close()

    conn = sqlite3.connect(path)
    try:
        (count,) = conn.execute(
            'SELECT "totalDelegations" FROM projection_delegation_summary'
        ).fetchone()
    finally:
        conn.close()
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_totals_ignore_a_synthetic_run_and_runs_still_lists_it() -> None:
    async with _provisioned_runner() as (_runner, conn, schema):
        registry_migration = (
            _MIGRATIONS_DIR.parent.parent
            / "node_projection_tenant_registry/migrations/0000_create_tenant_registry_mirror.sql"
        )
        await conn.execute(registry_migration.read_text())
        await conn.execute(
            "INSERT INTO tenant_registry_mirror (tenant_slug, tenant_uuid, status) "
            "VALUES ($1, $2, 'active')",
            PROJECTION_TENANT_SLUG,
            PROJECTION_TENANT_UUID,
        )
        tenant = str(PROJECTION_TENANT_UUID)
        pg = PostgresSyncProjectionAdapter(_base_dsn(), schema=schema)
        import asyncio

        async def write(payload: dict[str, Any]) -> None:
            await asyncio.to_thread(
                HandlerProjectionDelegation().handle, {**payload, "_db": pg}
            )

        async def totals() -> tuple[Any, Any]:
            summary = await conn.fetchrow(
                'SELECT "totalDelegations", "totalSavingsUsd", "qualityGateTotal", '
                '"byTaskType", "byModel" FROM projection_delegation_summary '
                "WHERE tenant_id::text = $1",
                tenant,
            )
            rows = await conn.fetch(
                RECORDS_SQL.replace("public.", f"{schema}."), tenant
            )
            records = sorted(
                (
                    r.model_dump()
                    for r in map(metering_record_from_row, map(dict, rows))
                    if r is not None
                ),
                key=lambda r: r["correlation_id"],
            )
            return dict(summary), records

        real = _terminal(None, _REAL_SAVINGS)
        real["tenant_id"] = tenant
        await write(real)
        before = await totals()
        assert before[0]["totalDelegations"] == 1
        assert len(before[1]) == 1

        synthetic = _terminal("synthetic", _SYNTHETIC_SAVINGS)
        synthetic["tenant_id"] = tenant
        await write(synthetic)
        # Positive control: the synthetic run is stored, typed experiment.
        stored = {
            r["correlation_id"]: r["data_source"]
            for r in await conn.fetch(
                "SELECT correlation_id::text AS correlation_id, data_source "
                "FROM delegation_events"
            )
        }
        assert stored == {
            real["correlation_id"]: "real",
            synthetic["correlation_id"]: "experiment",
        }

        assert await totals() == before
        # ... and the table the Runs listing reads still holds both runs.
        assert await conn.fetchval("SELECT COUNT(*) FROM delegation_events") == 2
