# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20008 AC5: metering-summary.v1 serves savings as a share of the baseline.

The Overview's "N% below the <baseline> baseline (modelled)" line may not
divide money in the browser, so the fold writes savings_usd / counterfactual_usd
next to savings_usd, as decimal text to the millionth, and the exposure serves
it. Failure modes, each with a test below:

P1  the share is savings_usd over counterfactual_usd, rounded half-even to the
    millionth, never the inverse and never spend over counterfactual;
P2  a zero counterfactual has no share: null, never a division error, 0 or 1;
P3  an unknown saving or an unknown counterfactual has no share: null;
P4  a saving below zero (spend above the baseline) is served as it is, never
    clamped to 0 or made positive;
P5  every window carries the field, and each window divides its own figures,
    not the all row's;
P6  an unresolved baseline serves null on every row;
P7  a replayed fold is byte-identical with the new field;
P8  the SQLite store keeps the decimal text and the served read returns it,
    with null served as a null key, not a missing one;
P9  the exposure declares the column, so the dashboard can bind it;
P10 the migration adds exactly one nullable column with no DEFAULT;
P11 a store written before the column, including one that already recorded the
    OMN-20226 store step, gains it once on open and keeps its rows.

The real-Postgres write is in tests/test_omn19977_metering_summary_real_postgres.py
and the SQLite/Postgres parity in tests/test_omn19968_writer_store_parity.py.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from omnimarket.nodes.node_projection_metering_summary.handlers import (
    handler_projection_metering_summary as fold_module,
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
from omnimarket.projection import sqlite_database
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

FIELD = "savings_pct_of_counterfactual"
METERING = "onex.snapshot.projection.metering-summary.v1"
TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary"
)
CONTRACT = NODE / "contract.yaml"
MIGRATION = NODE / "migrations/0004_metering_summary_savings_pct.sql"


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


# Prices are 1 per 1k in and 2 per 1k out, so a run's counterfactual is
# tokens_in / 1000 + 2 * tokens_out / 1000.
RECORDS = (
    # 09-25: counterfactual 1.4 + 1.0 = 2.4, spend 0.01, saving 2.39.
    _run("d25-a", NOW - timedelta(days=3), 1000, 200, "0"),
    _run("d25-b", NOW - timedelta(days=3) + timedelta(minutes=1), 800, 100, "0.01"),
    # 09-26: a measured run with no tokens, so the counterfactual is exactly 0.
    _run("d26-zero", NOW - timedelta(days=2), 0, 0, "0"),
    # 09-27: counterfactual 1, spend 2, saving -1.
    _run("d27-over", NOW - timedelta(days=1), 1000, 0, "2"),
    # 09-28: nothing measured, so no saving and no counterfactual.
    _run("d28-unmeasured", NOW - timedelta(hours=1)),
)

# Hand-computed: each window's saving over its own counterfactual.
EXPECTED = {
    ("all", ""): "0.408824",  # 1.39 / 3.4 = 0.40882352...
    ("day", "2026-09-25"): "0.995833",  # 2.39 / 2.4 = 0.99583333...
    ("day", "2026-09-26"): None,  # counterfactual 0
    ("day", "2026-09-27"): "-1.000000",  # -1 / 1
    ("day", "2026-09-28"): None,  # nothing measured
}


def _fold(
    baseline: ModelCounterfactualBaseline | None = None,
) -> dict[tuple[str, str], ModelMeteringSummaryRow]:
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=TENANT,
                records=RECORDS,
                baseline=baseline,
                baseline_model="baseline-model",
                as_of=NOW,
            )
        )
        .rows
    )
    return {(r.window_kind, r.window_start): r for r in rows}


def _share(savings: str | None, counterfactual: str | None) -> str | None:
    result: str | None = fold_module.savings_pct_of_counterfactual(
        None if savings is None else Decimal(savings),
        None if counterfactual is None else Decimal(counterfactual),
    )
    return result


@pytest.mark.parametrize(
    ("savings", "counterfactual", "expected"),
    [
        ("0.42", "1", "0.420000"),
        ("2.9", "3", "0.966667"),
        ("1", "4", "0.250000"),
        ("0.0000125", "0.0001", "0.125000"),
        ("3", "3", "1.000000"),
    ],
)
def test_p1_the_share_is_savings_over_counterfactual_to_the_millionth(
    savings: str, counterfactual: str, expected: str
) -> None:
    assert _share(savings, counterfactual) == expected


def test_p1_rounding_is_half_even_at_the_millionth() -> None:
    assert _share("0.0000005", "1") == "0.000000"
    assert _share("0.0000015", "1") == "0.000002"


@pytest.mark.parametrize("savings", ["0", "-0.01", "5"])
def test_p2_a_zero_counterfactual_has_no_share(savings: str) -> None:
    assert _share(savings, "0") is None


def test_p3_an_unknown_saving_or_counterfactual_has_no_share() -> None:
    assert _share(None, "1") is None
    assert _share("1", None) is None
    assert _share(None, None) is None


def test_p4_a_negative_saving_is_served_as_it_is() -> None:
    assert _share("-1", "1") == "-1.000000"
    assert _share("-0.5", "2") == "-0.250000"


def test_p5_every_window_divides_its_own_figures() -> None:
    rows = _fold(_baseline())
    assert set(rows) == set(EXPECTED)
    for key, row in rows.items():
        assert row.model_dump(mode="json")[FIELD] == EXPECTED[key], key


def test_p6_an_unresolved_baseline_serves_null_on_every_row() -> None:
    rows = _fold(None)
    assert len(rows) == len(EXPECTED)
    for key, row in rows.items():
        assert row.savings_usd is None, key
        assert row.model_dump(mode="json")[FIELD] is None, key


def test_p7_a_replayed_fold_is_byte_identical() -> None:
    first = [r.model_dump_json() for r in _fold(_baseline()).values()]
    second = [r.model_dump_json() for r in _fold(_baseline()).values()]
    assert first == second
    assert any(f'"{FIELD}":"0.408824"' in row for row in first)


def test_p8_the_sqlite_store_keeps_and_serves_the_share(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    store_rows(SqliteDatabaseAdapter(db_path), tuple(_fold(_baseline()).values()))
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=TENANT)
    )
    response = client.get(f"/projection/{METERING}")
    assert response.status_code == 200, response.json()
    served = {
        (row["window_kind"], row["window_start"]): row
        for row in response.json()["rows"]
    }
    assert set(served) == set(EXPECTED)
    for key, row in served.items():
        assert FIELD in row, key
        assert row[FIELD] == EXPECTED[key], (key, row[FIELD])


def test_p9_the_exposure_declares_the_column() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (exposure,) = [
        e for e in contract["projection_api"]["exposures"] if e["topic"] == METERING
    ]
    assert FIELD in exposure["columns"]


def test_p10_the_migration_adds_one_nullable_column_without_a_default() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    added = re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)\s+(\w+)", sql)
    assert added == [(FIELD, "TEXT")]
    statements = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).upper()
    assert "DEFAULT" not in statements
    assert "NOT NULL" not in statements


def _old_store(db_path: Path, *, recorded_steps: tuple[str, ...]) -> None:
    """A store as a build before the column wrote it, holding one row."""
    ddl = sqlite_database._METERING_SUMMARY_DDL
    lines = [line for line in ddl.splitlines() if FIELD not in line]
    old_ddl = "\n".join(lines) + "\n"
    row = _fold(_baseline())[("all", "")].model_dump(mode="json")
    row.pop(FIELD)
    with sqlite3.connect(db_path) as conn:
        conn.execute(old_ddl)
        conn.execute(
            f"INSERT INTO metering_summary ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        conn.execute(sqlite_database._STORE_STEPS_DDL)
        for step in recorded_steps:
            conn.execute(
                f"INSERT INTO {sqlite_database._STORE_STEPS_TABLE} "
                "(step, applied_at) VALUES (?, ?)",
                (step, NOW.isoformat()),
            )


@pytest.mark.parametrize(
    "recorded_steps",
    [(), (sqlite_database._METERING_SUMMARY_MEASURES_STEP,)],
    ids=["no-steps-recorded", "omn20226-step-recorded"],
)
def test_p11_an_old_store_gains_the_column_once_and_keeps_its_rows(
    tmp_path: Path, recorded_steps: tuple[str, ...]
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _old_store(db_path, recorded_steps=recorded_steps)
    for _ in range(3):
        rows = SqliteDatabaseAdapter(db_path).query(
            "metering_summary", {"tenant_id": TENANT}
        )
    with sqlite3.connect(db_path) as conn:
        names = [row[1] for row in conn.execute("PRAGMA table_info(metering_summary)")]
    assert names.count(FIELD) == 1, names
    [row] = rows
    assert row[FIELD] is None
    assert row["savings_usd"] == "1.39"
