# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20007: the daily savings series is served from metering-summary.v1.

Under the 2026-10-04 ruling the pages read the daily series from the ``day``
rows of ``node_projection_metering_summary``, and savings-series.v1 stays in
the catalogue as not served. These tests fold recorded runs for two tenants,
store the rows in a local SQLite store the way the local writers do, and read
them back through the projection read node exactly as ``onex dashboard`` does,
with the exposures taken from the node contracts themselves (no hand-built
topic map), so the declaration is what is under test.

Failure modes each test is written against:

* the exposure is absent: the read answers 404 ``unknown_topic``;
* it is served but not tenant-scoped: another tenant's rows come back;
* the contract names a column the table lacks (503
  ``projection_column_missing``), or leaves a column out of the served row;
* the served day rows do not add up to the served all row;
* savings-series.v1 is wrongly flipped to ok, or starts answering 200;
* the key grain is wrong, so a consumer keying on it collapses rows: the
  declared key must be the store's own unique key;
* the cursor cannot walk the rows, so a paged read skips or repeats one
  (exact within one baseline model; the gap across two baseline models is
  measured and pinned below, not hidden);
* a gate exemption instead of a real input: the declaration is read from the
  contract, never from a test-local topic map.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    store_rows,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

METERING = "onex.snapshot.projection.metering-summary.v1"
SAVINGS_SERIES = "onex.snapshot.projection.delegation.savings-series.v1"
TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
AS_OF = datetime(2026, 9, 28, 12, tzinfo=UTC)
IDLE_DAY = date(2026, 9, 26)
BASELINE_MODEL = "baseline-model"
BASELINE = ModelCounterfactualBaseline(
    model=BASELINE_MODEL,
    price_in_per_1k=Decimal("3"),
    price_out_per_1k=Decimal("15"),
    as_of="2026-09-01",
    pricing_manifest_version="1",
    source="pricing_manifest",
)
# Every column of metering_summary: the served row must carry each of them.
ROW_COLUMNS = frozenset(ModelMeteringSummaryRow.model_fields)


def _run(
    cid: str,
    stamp: str,
    tokens_in: int | None,
    tokens_out: int | None,
    spend: str | None,
) -> ModelMeteringRecord:
    return ModelMeteringRecord(
        correlation_id=cid,
        occurred_at=datetime.fromisoformat(stamp),
        model="local-a",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        spend_usd=None if spend is None else Decimal(spend),
    )


RECORDS: dict[str, tuple[ModelMeteringRecord, ...]] = {
    TENANT: (
        _run("t1-25", "2026-09-25T09:00:00+00:00", 1000, 200, "0.1"),
        _run("t1-25-late", "2026-09-25T23:59:59.999999+00:00", 400, 100, "0.2"),
        # 2026-09-26 is idle; it is requested, so it has an empty row.
        _run("t1-27", "2026-09-27T00:00:00+00:00", 2000, 300, "0.3"),
        _run("t1-27-unmeasured", "2026-09-27T08:00:00+00:00", None, None, None),
        _run("t1-28", "2026-09-28T11:59:59.999999+00:00", 300, 30, "0.05"),
    ),
    OTHER_TENANT: (
        _run("t2-24", "2026-09-24T10:00:00+00:00", 50_000, 9_000, "4"),
        _run("t2-27", "2026-09-27T10:00:00+00:00", 7_000, 1_000, "2"),
    ),
}


def _fold(tenant: str) -> tuple[ModelMeteringSummaryRow, ...]:
    records = RECORDS[tenant]
    days = frozenset({IDLE_DAY}) | {
        r.occurred_at.astimezone(UTC).date() for r in records
    }
    return (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=tenant,
                records=records,
                baseline=BASELINE,
                baseline_model=BASELINE_MODEL,
                as_of=AS_OF,
                days=days,
            )
        )
        .rows
    )


def _store(tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    for tenant in (TENANT, OTHER_TENANT):
        store_rows(adapter, _fold(tenant))
    return db_path


@cache
def _topics() -> dict[str, ProjectionTableConfig]:
    """The exposures exactly as the node contracts declare them."""
    return build_projection_topic_map()


def _client(db_path: Path, tenant: str = TENANT) -> TestClient:
    topics = _topics()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    return TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )


def _read(db_path: Path, tenant: str = TENANT, query: str = "") -> dict[str, Any]:
    response = _client(db_path, tenant).get(f"/projection/{METERING}{query}")
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def _money(values: list[str | None]) -> Decimal | None:
    """Exact sum of the known values; no known value is no figure, not zero."""
    known = [Decimal(v) for v in values if v is not None]
    return sum(known, Decimal("0")) if known else None


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        row["tenant_id"],
        row["window_kind"],
        row["window_start"],
        row["baseline_model"],
    )


# -- served, scoped, complete -------------------------------------------------


def test_the_day_rows_are_served_for_the_tenant_and_sum_to_the_served_all_row(
    tmp_path: Path,
) -> None:
    db_path = _store(tmp_path)
    body = _read(db_path)
    rows = body["rows"]

    assert body["tenant"] == TENANT
    assert {row["tenant_id"] for row in rows} == {TENANT}
    (all_row,) = [r for r in rows if r["window_kind"] == "all"]
    days = [r for r in rows if r["window_kind"] == "day"]
    assert sorted(d["window_start"] for d in days) == [
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-09-28",
    ]
    for field in ("spend_usd", "counterfactual_usd", "savings_usd"):
        assert _money([d[field] for d in days]) == _dec(all_row[field]), field
    for field in ("runs_total", "runs_measured", "tokens_in", "tokens_out"):
        assert sum(d[field] for d in days) == all_row[field], field
    # Hand-computed: spend 0.1 + 0.2 + 0.3 + 0.05; counterfactual at 3/1k in
    # and 15/1k out over 3700 in and 630 out = 11.1 + 9.45.
    assert _dec(all_row["spend_usd"]) == Decimal("0.65")
    assert _dec(all_row["counterfactual_usd"]) == Decimal("20.55")
    assert _dec(all_row["savings_usd"]) == Decimal("19.90")
    assert all_row["runs_total"] == 5
    assert all_row["runs_measured"] == 4
    assert body["as_of"] == AS_OF.isoformat()


def test_the_served_rows_are_the_stored_rows_with_every_column(
    tmp_path: Path,
) -> None:
    db_path = _store(tmp_path)
    rows = _read(db_path)["rows"]
    stored = {
        _key(r): r for r in (row.model_dump(mode="json") for row in _fold(TENANT))
    }
    assert len(rows) == len(stored)
    for row in rows:
        assert set(row) == ROW_COLUMNS
        expected = dict(stored[_key(row)])
        # summary_json is served decoded; every other column byte for byte.
        assert row.pop("summary_json") is not None
        expected.pop("summary_json")
        assert row == expected


def test_another_tenants_rows_never_come_back(tmp_path: Path) -> None:
    db_path = _store(tmp_path)
    own = _read(db_path, tenant=OTHER_TENANT)["rows"]
    assert {row["tenant_id"] for row in own} == {OTHER_TENANT}
    (all_row,) = [r for r in own if r["window_kind"] == "all"]
    assert _dec(all_row["spend_usd"]) == Decimal("6")
    # Naming the other tenant from this install is refused, not served.
    response = _client(db_path).get(f"/projection/{METERING}?tenant={OTHER_TENANT}")
    assert response.status_code == 422
    assert "rows" not in response.json()


@pytest.mark.asyncio
async def test_the_read_node_scopes_by_tenant_without_the_dashboard(
    tmp_path: Path,
) -> None:
    db_path = _store(tmp_path)
    handler = HandlerProjectionRead(
        topic_map=_topics(), row_source=SqliteTableRowSource(db_path)
    )
    for tenant in (TENANT, OTHER_TENANT):
        result = await handler.handle(
            ModelProjectionReadRequest(topic=METERING, tenant_id=tenant)
        )
        assert result.ok, result.error
        assert {row["tenant_id"] for row in result.rows} == {tenant}
    unscoped = await handler.handle(ModelProjectionReadRequest(topic=METERING))
    assert not unscoped.ok
    assert unscoped.http_status == 422


def test_a_replayed_fold_serves_the_same_rows(tmp_path: Path) -> None:
    db_path = _store(tmp_path)
    first = _read(db_path)["rows"]
    store_rows(SqliteDatabaseAdapter(db_path), _fold(TENANT))
    assert _read(db_path)["rows"] == first


def test_a_paged_read_walks_every_row_once(tmp_path: Path) -> None:
    db_path = _store(tmp_path)
    everything = {_key(r) for r in _read(db_path)["rows"]}
    seen = _walk(db_path, limit=2)
    assert sorted(seen) == sorted(everything)
    assert len(seen) == len(everything)


def _walk(db_path: Path, limit: int) -> list[tuple[str, str, str, str]]:
    seen: list[tuple[str, str, str, str]] = []
    query = f"?limit={limit}"
    for _ in range(100):
        body = _read(db_path, query=query)
        seen.extend(_key(r) for r in body["rows"])
        if body["next_cursor"] is None:
            return seen
        query = f"?limit={limit}&since={body['next_cursor']}"
    raise AssertionError("the walk did not end")


def test_known_gap_a_page_boundary_between_two_baselines_skips_a_row(
    tmp_path: Path,
) -> None:
    """Pinned gap, measured 2026-10-04: ``window_start`` is the cursor and it is
    unique per tenant and baseline model only. ``metering_summary`` has no
    column unique per tenant, so an exact walk across baseline models needs a
    cursor column in the table (the fold's migration, not this exposure).

    Population: one tenant, two baseline models, 9 rows, 4 window_start values
    shared by both. A one-row page always lands on a tie, so the walk skips the
    second row of each of the 4; a page at the contract limit holds all 9.
    When the table gains a unique cursor, this test fails: switch the exposure
    to it and assert the walk is exact instead.
    """
    db_path = _store(tmp_path)
    second = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=TENANT,
                records=RECORDS[TENANT],
                baseline=None,
                baseline_model="baseline-b",
                as_of=AS_OF,
            )
        )
        .rows
    )
    store_rows(SqliteDatabaseAdapter(db_path), second)
    everything = {_key(r) for r in _read(db_path)["rows"]}
    assert len(everything) == 9

    missed = everything - set(_walk(db_path, limit=1))
    shared = {k[2] for k in everything if k[3] == "baseline-b"}
    assert len(missed) == 4
    assert {k[2] for k in missed} == shared
    assert set(_walk(db_path, limit=500)) == everything


# -- the declaration ------------------------------------------------------------


def test_the_declared_key_is_the_stores_unique_key(tmp_path: Path) -> None:
    cfg = _topics()[METERING]
    assert cfg.key_grain == "mutable"
    conn = sqlite3.connect(_store(tmp_path))
    try:
        unique_key = tuple(
            str(row[2])
            for row in conn.execute("PRAGMA index_info('metering_summary_key')")
        )
    finally:
        conn.close()
    assert unique_key == ("tenant_id", "window_kind", "window_start", "baseline_model")
    assert cfg.key_columns == unique_key


# -- the catalogue ------------------------------------------------------------------


def test_the_catalogue_lists_metering_ok_and_savings_series_not_served(
    tmp_path: Path,
) -> None:
    client = _client(_store(tmp_path))
    catalogue = {
        row["topic"]: row for row in client.get("/projections").json()["topics"]
    }

    metering = catalogue[METERING]
    assert metering["status"] == "ok"
    assert metering["bus_backed"] is True
    assert metering["degraded_reason"] is None
    assert metering["tenant_scoped"] is True
    assert metering["tenant_column"] == "tenant_id"
    assert set(metering["columns"]) == ROW_COLUMNS

    savings = catalogue[SAVINGS_SERIES]
    assert savings["status"] == "degraded"
    assert savings["bus_backed"] is False
    assert savings["degraded_reason"] == "not_yet_bus_backed"
    response = client.get(f"/projection/{SAVINGS_SERIES}")
    assert response.status_code == 503
    assert response.json()["error"] == "not_yet_bus_backed"
