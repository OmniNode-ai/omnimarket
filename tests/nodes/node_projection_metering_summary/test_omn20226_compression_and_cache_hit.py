# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20226: metering-summary.v1 serves Compression and Cache hit rate.

The Overview's Compression (raw over compressed input tokens) and Cache hit
rate (calls answered from the semantic cache over all calls) must be served
figures, never computed in the browser. Nothing on the delegate path measures
either today: no record carries a raw or compressed token count, and no record
says a call was answered from a semantic cache. So every row serves the three
fields as null, which the dashboard renders as its typed not-measured state.

Failure modes, each with a test below:

C1  measured runs (tokens and spend known) still serve null for all three
    fields: a known token count is not a compression measurement, and a run is
    not a cache miss because nobody said it was a hit;
C2  null, never a default: not 0, not "0", not 1.0 and not "1.00x";
C3  every window carries the fields, day rows and the all row;
C4  a row whose runs are all unmeasured serves null too;
C5  a replayed fold stays byte-identical with the new fields;
C6  the fields survive the SQLite store and come back through the served
    metering-summary.v1 read as null keys, not missing keys;
C7  the exposure declares all three columns, so the dashboard can bind them;
C8  the migration adds nullable columns with no DEFAULT, so rows written before
    a producer exists read null rather than a made-up zero.

The store-upgrade and dashboard-startup cases (C9-C13) live in
test_omn20226_existing_store_upgrade.py, so this file stays inside the bound
evidence check's time budget.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
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
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

FIELDS = ("compression_ratio", "cache_hit_rate", "runs_cache_answered")
METERING = "onex.snapshot.projection.metering-summary.v1"
TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary"
)
CONTRACT = NODE / "contract.yaml"
MIGRATION = NODE / "migrations/0003_metering_summary_compression_and_cache_hit.sql"


def _baseline() -> ModelCounterfactualBaseline:
    return ModelCounterfactualBaseline(
        model="baseline-model",
        price_in_per_1k=Decimal("1"),
        price_out_per_1k=Decimal("2"),
        as_of="2026-09-01",
        pricing_manifest_version="1",
        source="pricing_manifest",
    )


def _run(
    cid: str,
    at: datetime,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    spend: str | None = None,
) -> ModelMeteringRecord:
    return ModelMeteringRecord(
        correlation_id=cid,
        occurred_at=at,
        model="local-a",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        spend_usd=None if spend is None else Decimal(spend),
    )


# Yesterday: two fully measured runs. Today: one run with nothing measured.
RECORDS = (
    _run("y1", YESTERDAY, 1000, 200, "0"),
    _run("y2", YESTERDAY + timedelta(minutes=1), 800, 100, "0.01"),
    _run("t1-unmeasured", NOW - timedelta(hours=1)),
)


def _fold(
    records: tuple[ModelMeteringRecord, ...] = RECORDS,
) -> dict[tuple[str, str], ModelMeteringSummaryRow]:
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=TENANT,
                records=records,
                baseline=_baseline(),
                baseline_model="baseline-model",
                as_of=NOW,
            )
        )
        .rows
    )
    return {(r.window_kind, r.window_start): r for r in rows}


def _values(row: ModelMeteringSummaryRow) -> dict[str, Any]:
    dumped = row.model_dump(mode="json")
    return {field: dumped[field] for field in FIELDS}


def test_c1_c2_measured_runs_serve_null_not_a_default() -> None:
    yesterday = _fold()[("day", YESTERDAY.date().isoformat())]
    assert yesterday.runs_measured == 2
    assert _values(yesterday) == dict.fromkeys(FIELDS)


def test_c3_every_window_carries_the_three_fields_as_null() -> None:
    rows = _fold()
    assert ("all", "") in rows
    assert len(rows) == 3  # two day rows and the all row
    for key, row in rows.items():
        assert _values(row) == dict.fromkeys(FIELDS), key


def test_c4_an_all_unmeasured_row_serves_null() -> None:
    today = _fold()[("day", NOW.date().isoformat())]
    assert today.runs_measured == 0
    assert _values(today) == dict.fromkeys(FIELDS)


def test_c5_a_replayed_fold_is_byte_identical() -> None:
    first = [r.model_dump_json() for r in _fold().values()]
    second = [r.model_dump_json() for r in _fold().values()]
    assert first == second
    assert all('"compression_ratio":null' in row for row in first)


def test_c6_the_fields_are_stored_and_served_as_null_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    store_rows(SqliteDatabaseAdapter(db_path), tuple(_fold().values()))
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=TENANT)
    )
    response = client.get(f"/projection/{METERING}")
    assert response.status_code == 200, response.json()
    served = response.json()["rows"]
    assert len(served) == 3
    for row in served:
        for field in FIELDS:
            assert field in row, field
            assert row[field] is None, (field, row[field])


def test_c7_the_exposure_declares_the_three_columns() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (exposure,) = [
        e for e in contract["projection_api"]["exposures"] if e["topic"] == METERING
    ]
    for field in FIELDS:
        assert field in exposure["columns"], field


def test_c8_the_migration_adds_nullable_columns_without_a_default() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    added = dict(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)\s+(\w+)", sql))
    assert set(added) == set(FIELDS)
    assert added["compression_ratio"] == "TEXT"
    assert added["cache_hit_rate"] == "TEXT"
    assert added["runs_cache_answered"] == "INTEGER"
    statements = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).upper()
    assert "DEFAULT" not in statements
    assert "NOT NULL" not in statements
