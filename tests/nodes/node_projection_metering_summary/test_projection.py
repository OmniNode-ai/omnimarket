# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977: durable, deterministic metering projections."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_metering import metering_command
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
    store_rows,
)
from omnimarket.pricing import resolve_baseline_model
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_summary import (
    read_summary_row,
    refresh_metering_summary,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def baseline(model: str = "model-a", version: str = "1") -> ModelCounterfactualBaseline:
    return ModelCounterfactualBaseline(
        model=model,
        price_in_per_1k=Decimal("1"),
        price_out_per_1k=Decimal("2"),
        as_of="2026-09-01",
        pricing_manifest_version=version,
        source="pricing_manifest",
    )


def request() -> ModelMeteringSummaryFoldRequest:
    return ModelMeteringSummaryFoldRequest(
        tenant_id="local",
        baseline_model="model-a",
        baseline=baseline(),
        as_of=NOW,
        records=(
            ModelMeteringRecord(
                correlation_id="a",
                occurred_at=NOW - timedelta(days=1),
                model="worker",
                tokens_in=1000,
                tokens_out=100,
                spend_usd=Decimal("0.2"),
            ),
            ModelMeteringRecord(
                correlation_id="b", occurred_at=NOW - timedelta(hours=1)
            ),
            ModelMeteringRecord(
                correlation_id="c", occurred_at=NOW - timedelta(hours=2), tokens_in=50
            ),
        ),
    )


def mint_tenant(path: Path) -> str:
    """OMN-17427: the CLI keys its rows on the install's own minted identity."""
    return str(mint_local_tenant_identity(db_path=path).tenant_uuid)


def seed(path: Path) -> None:
    db = SqliteDatabaseAdapter(path)
    for record in request().records:
        db.upsert(
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": record.correlation_id,
                "created_at": record.occurred_at.isoformat(),
                "delegated_to": record.model,
                "model_name": record.model,
                "task_type": "",
                "cost_savings_usd": None,
                "tokens_input": record.tokens_in or 0,
                "tokens_output": record.tokens_out or 0,
                "cost_usd": str(record.spend_usd)
                if record.spend_usd is not None
                else None,
            },
        )


def test_fold_and_store_are_byte_identical(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "db.sqlite")
    fold = HandlerProjectionMeteringSummary()
    first = fold.handle(request()).rows
    store_rows(db, first)
    stored = db.query("metering_summary", order_by="window_start")
    second = fold.handle(request()).rows
    store_rows(db, second)
    assert first == second
    assert stored == db.query("metering_summary", order_by="window_start")
    assert len(stored) == 3
    for row in first:
        assert row.summary_json == json.dumps(
            json.loads(row.summary_json), sort_keys=True, separators=(",", ":")
        )
        assert row.model_dump(mode="json") in stored


def test_unknown_measurements_do_not_become_money() -> None:
    row = next(
        r
        for r in HandlerProjectionMeteringSummary().handle(request()).rows
        if r.window_kind == "all"
    )
    assert (
        row.runs_total,
        row.runs_measured,
        row.runs_unknown_tokens,
        row.runs_unknown_spend,
    ) == (3, 1, 1, 1)
    assert row.spend_usd is not None
    assert row.savings_usd is not None
    assert Decimal(row.spend_usd) == Decimal("0.2")
    assert Decimal(row.savings_usd) == Decimal("1.0")


def test_unresolved_preserves_requested_model() -> None:
    req = request().model_copy(update={"baseline": None, "baseline_model": "absent"})
    for row in HandlerProjectionMeteringSummary().handle(req).rows:
        assert row.baseline_model == "absent"
        assert row.baseline_state == EnumBaselineState.UNRESOLVED
        assert row.savings_usd is None


def test_refresh_baseline_changes_every_window_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db.sqlite"
    seed(path)
    monkeypatch.setattr(
        "omnimarket.projection.sqlite_metering_summary.resolve_baseline",
        lambda model: baseline(model, "2" if model == "model-b" else "1"),
    )
    first = refresh_metering_summary(path, "local", "model-a", NOW)
    second = refresh_metering_summary(
        path, "local", "model-b", NOW + timedelta(hours=1)
    )
    assert len(first) == len(second) == 3
    assert all(
        r.baseline_model == "model-b"
        and r.pricing_manifest_version == "2"
        and r.as_of == (NOW + timedelta(hours=1)).isoformat()
        for r in second
    )
    db = SqliteDatabaseAdapter(path)
    count = len(db.query("metering_summary"))
    third = refresh_metering_summary(path, "local", "model-b", NOW + timedelta(hours=2))
    assert len(db.query("metering_summary")) == count
    assert read_summary_row(path, "local", "all", "", "model-b") in third


def test_cli_json_is_stored_content_and_keys(tmp_path: Path) -> None:
    """OMN-19977: the CLI prints the stored row (its columns, with summary_json
    spread) and reads it as stored: a second read is byte-identical."""
    path = tmp_path / "db.sqlite"
    seed(path)
    tenant = mint_tenant(path)
    baseline_model = resolve_baseline_model(overlay={}, store={}).model
    refresh_metering_summary(path, tenant, baseline_model, NOW)
    result = CliRunner().invoke(metering_command, ["--db", str(path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    row = read_summary_row(path, tenant, "all", "", baseline_model)
    assert row is not None
    expected = json.loads(row.summary_json)
    expected.update(
        {
            key: value
            for key, value in row.model_dump(mode="json").items()
            if key != "summary_json"
        }
    )
    assert payload == expected
    again = CliRunner().invoke(metering_command, ["--db", str(path), "--json"])
    assert again.stdout == result.stdout


def test_dispatch_capability_only_on_writer() -> None:
    assert MeteringSummaryProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert not getattr(
        HandlerProjectionMeteringSummary, "onex_runtime_inprocess_dispatch", False
    )


def test_utc_boundaries_and_explicit_empty_day() -> None:
    req = request().model_copy(update={"days": frozenset({date(2026, 9, 20)})})
    rows = HandlerProjectionMeteringSummary().handle(req).rows
    assert [(r.window_kind, r.window_start) for r in rows] == [
        ("all", ""),
        ("day", "2026-09-20"),
    ]
    assert rows[1].runs_total == 0
    assert rows[1].spend_usd is None
    assert rows[0].runs_total == 3


def test_day_boundaries_use_utc_and_exclude_as_of() -> None:
    records = tuple(
        ModelMeteringRecord(
            correlation_id=str(i), occurred_at=datetime.fromisoformat(stamp)
        )
        for i, stamp in enumerate(
            ("2026-09-27T23:59:59+00:00", "2026-09-27T20:00:00-04:00", NOW.isoformat())
        )
    )
    req = request().model_copy(update={"records": records})
    rows = HandlerProjectionMeteringSummary().handle(req).rows
    assert [(r.window_start, r.runs_total) for r in rows] == [
        ("", 2),
        ("2026-09-27", 1),
        ("2026-09-28", 1),
    ]
    reversed_req = req.model_copy(update={"records": tuple(reversed(records))})
    assert HandlerProjectionMeteringSummary().handle(reversed_req).rows == rows
    assert rows[1].window_end == "2026-09-28T00:00:00+00:00"


def test_cli_manifest_change_is_reported_stale_not_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-19977: a manifest change is the refresh command's job (ruling D-A);
    the CLI names the stale row and leaves every stored row as it was."""
    path = tmp_path / "db.sqlite"
    seed(path)
    tenant = mint_tenant(path)
    current = baseline()
    monkeypatch.setattr(
        "omnimarket.projection.sqlite_metering_summary.resolve_baseline",
        lambda _model: current,
    )
    refresh_metering_summary(path, tenant, "model-a", NOW)
    args = ["--db", str(path), "--baseline", "model-a", "--json"]
    runner = CliRunner()
    assert runner.invoke(metering_command, args).exit_code == 0
    current = baseline(version="new-manifest")
    result = runner.invoke(metering_command, args)
    assert result.exit_code == 1, result.output
    state = json.loads(result.stdout)
    assert state["state"] == "METERING_SUMMARY_STALE"
    assert state["current_pricing_manifest_version"] == "new-manifest"
    stored = SqliteDatabaseAdapter(path).query("metering_summary")
    assert len(stored) == 3
    assert all(row["pricing_manifest_version"] == "1" for row in stored)


def test_cli_explicit_day_reads_its_stored_row(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    seed(path)
    tenant = mint_tenant(path)
    refresh_metering_summary(
        path, tenant, resolve_baseline_model(overlay={}, store={}).model, NOW
    )
    result = CliRunner().invoke(
        metering_command, ["--db", str(path), "--day", "2026-09-27", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["window_kind"] == "day"
    assert payload["window_start"] == "2026-09-27"
    assert payload["runs_total"] == 1
    row = read_summary_row(path, tenant, "day", "2026-09-27", payload["baseline_model"])
    assert row is not None
    assert json.loads(row.summary_json)["runs_total"] == payload["runs_total"]
