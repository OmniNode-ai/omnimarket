# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20009: metering-summary.v1 serves the average saving per measured run.

The Overview's "Avg saving / call" is savings over calls from the one savings
definition. The dashboard may not divide, so each metering_summary row carries
``savings_per_measured_run_usd``: the row's ``savings_usd`` over its
``runs_measured``, as decimal text rounded half-even to a millionth of a
dollar. Only measured runs are in the denominator, because only they are in
``savings_usd``. It is null, never zero, when the row has no saving.

Failure modes, each with a test below:

G1  measured runs with a resolved baseline: the exact quotient, rounded;
G2  no measured run (unknown tokens or unknown spend only): null, not 0;
G3  an unresolved baseline: null, not 0, even with measured runs;
G4  unmeasured runs never enter the denominator;
G5  each day row and the all row carry their own value;
G6  a negative saving (the local run cost more) stays negative;
G7  a replayed fold gives byte-identical values;
G8  the value survives the SQLite store and comes back through the served
    metering-summary.v1 read exactly as stored;
G9  the Postgres writer writes the column (real-Postgres twin elsewhere).
"""

from __future__ import annotations

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

FIELD = "savings_per_measured_run_usd"
METERING = "onex.snapshot.projection.metering-summary.v1"
TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary/contract.yaml"
)


def _baseline() -> ModelCounterfactualBaseline:
    # 1 USD per 1k input tokens and 2 USD per 1k output tokens.
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


# Yesterday: three measured runs, counterfactual 1.0 each, spend 0, 0, 0.1 ->
# savings 2.9 over 3 measured = 0.9666666... -> 0.966667. One unmeasured run.
# Today: one run with unknown spend only (no measured run).
RECORDS = (
    _run("y1", YESTERDAY, 1000, 0, "0"),
    _run("y2", YESTERDAY + timedelta(minutes=1), 1000, 0, "0"),
    _run("y3", YESTERDAY + timedelta(minutes=2), 1000, 0, "0.1"),
    _run("y4-unknown-tokens", YESTERDAY + timedelta(minutes=3)),
    _run("t1-unknown-spend", NOW - timedelta(hours=1), 500, 0, None),
)


def _fold(
    records: tuple[ModelMeteringRecord, ...] = RECORDS,
    baseline: ModelCounterfactualBaseline | None = None,
) -> dict[tuple[str, str], ModelMeteringSummaryRow]:
    resolved = _baseline() if baseline is None else baseline
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=TENANT,
                records=records,
                baseline=resolved,
                baseline_model=resolved.model,
                as_of=NOW,
            )
        )
        .rows
    )
    return {(r.window_kind, r.window_start): r for r in rows}


def _value(row: ModelMeteringSummaryRow) -> Any:
    return row.model_dump(mode="json")[FIELD]


def test_g1_g4_all_row_is_savings_over_measured_runs_only() -> None:
    all_row = _fold()[("all", "")]
    assert all_row.runs_total == 5
    assert all_row.runs_measured == 3
    assert Decimal(str(all_row.savings_usd)) == Decimal("2.9")
    # 2.9 / 3, never 2.9 / 5 (runs_total) and never rounded to a cent.
    assert _value(all_row) == "0.966667"


def test_g2_a_row_with_no_measured_run_serves_null_not_zero() -> None:
    today = _fold()[("day", NOW.date().isoformat())]
    assert today.runs_total == 1
    assert today.runs_measured == 0
    assert _value(today) is None


def test_g3_an_unresolved_baseline_serves_null_not_zero() -> None:
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=TENANT,
                records=RECORDS,
                baseline=None,
                baseline_model="baseline-model",
                as_of=NOW,
            )
        )
        .rows
    )
    assert rows
    for row in rows:
        assert _value(row) is None, row.window_kind


def test_g5_each_window_carries_its_own_value() -> None:
    rows = _fold()
    yesterday = rows[("day", YESTERDAY.date().isoformat())]
    assert yesterday.runs_measured == 3
    assert _value(yesterday) == "0.966667"
    assert _value(rows[("day", NOW.date().isoformat())]) is None


def test_g6_a_negative_saving_stays_negative() -> None:
    # Counterfactual 1.0, spend 1.5 -> saving -0.5 over one measured run.
    rows = _fold((_run("costly", YESTERDAY, 1000, 0, "1.5"),))
    assert _value(rows[("all", "")]) == "-0.500000"


def test_g7_a_replayed_fold_is_byte_identical() -> None:
    first = [r.model_dump_json() for r in _fold().values()]
    second = [r.model_dump_json() for r in _fold().values()]
    assert first == second


def test_g8_the_value_is_stored_and_served_unchanged(tmp_path: Path) -> None:
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
    served = {(r["window_kind"], r["window_start"]): r for r in response.json()["rows"]}
    assert served[("all", "")][FIELD] == "0.966667"
    assert served[("day", NOW.date().isoformat())][FIELD] is None


def test_g8_the_exposure_declares_the_column() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (exposure,) = [
        e for e in contract["projection_api"]["exposures"] if e["topic"] == METERING
    ]
    assert FIELD in exposure["columns"]
