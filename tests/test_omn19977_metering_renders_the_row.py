# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977 AC2, AC6 and AC7 (CLI half): ``onex metering`` renders the row.

The stored ``metering-summary.v1`` row is the one definition of runs, tokens,
spend and savings. The end of every mode 1 ``onex delegate`` refreshes it; the
CLI only reads it, by the same baseline resolution the delegation used, and
says so when the row it would print is absent or older than the evidence.

Failure modes each test is written against:

* render refreshes, folds or writes (it used to call
  ``refresh_metering_summary`` on every read): the spy test, with the store's
  bytes compared before and after, and a positive control proving the spies
  fire when the fold really runs;
* a printed field differs from the served row, or the CLI prints a field the
  row does not carry: compared key by key with the row read through
  ``HandlerProjectionRead`` for the same tenant, window and baseline;
* no row: a typed ``METERING_SUMMARY_MISSING`` naming the repair, never a
  recomputed figure;
* a row older than a recorded run, or priced against another manifest version:
  a typed ``METERING_SUMMARY_STALE``;
* another tenant's row in the same store;
* ``--include-fixtures`` writing fixture-inclusive numbers under the real key;
* the default baseline frozen at import instead of resolved through
  ``resolve_baseline_model``; the default window not all-time;
* an unresolved baseline printed as a number; an unmeasured run summed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from omnimarket import pricing
from omnimarket.cli.cli_metering import metering_command
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.nodes.node_metering_summary_compute.handlers import (
    handler_metering_summary,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_projection_metering_summary.handlers import (
    handler_metering_summary_writer,
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
from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

METERING = "onex.snapshot.projection.metering-summary.v1"
AS_OF = datetime(2026, 9, 28, 12, tzinfo=UTC)
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
PRICED_BASELINE = "claude-opus-4-6"
UNPRICED_BASELINE = "no-such-model-omn19977"

RECORDS = (
    # measured, on 2026-09-27
    ("a", AS_OF - timedelta(days=1), 1000, 100, "0.2"),
    # unmeasured: no tokens, no spend
    ("b", AS_OF - timedelta(hours=1), 0, 0, None),
    # tokens but no spend
    ("c", AS_OF - timedelta(hours=2), 50, 0, None),
)


def _seed_events(path: Path, records: Any = RECORDS) -> None:
    db = SqliteDatabaseAdapter(path)
    for cid, at, tokens_in, tokens_out, spend in records:
        db.upsert(
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": cid,
                "created_at": at.isoformat(),
                "delegated_to": "worker",
                "model_name": "worker",
                "task_type": "",
                "cost_savings_usd": None,
                "tokens_input": tokens_in,
                "tokens_output": tokens_out,
                "cost_usd": spend,
            },
        )


def _other_tenant_rows(path: Path) -> None:
    """A second tenant's rows in the same store, with different numbers."""
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id=OTHER_TENANT,
                records=(
                    ModelMeteringRecord(
                        correlation_id="other",
                        occurred_at=AS_OF - timedelta(days=3),
                        model="worker",
                        tokens_in=9000,
                        tokens_out=900,
                        spend_usd=Decimal("7"),
                    ),
                ),
                baseline=None,
                baseline_model=pricing.resolve_baseline_model(
                    overlay={}, store={}
                ).model,
                as_of=AS_OF,
            )
        )
        .rows
    )
    handler_metering_summary_writer.store_rows(SqliteDatabaseAdapter(path), rows)


def _install(tmp_path: Path, *, refresh: bool = True) -> tuple[Path, str]:
    """A store with recorded runs, this install's identity, and (optionally)
    the rows the end-of-delegate refresh writes, under the resolved baseline."""
    path = tmp_path / "delegation.sqlite"
    _seed_events(path)
    tenant = str(mint_local_tenant_identity(db_path=path).tenant_uuid)
    _other_tenant_rows(path)
    if refresh:
        sqlite_metering_summary.refresh_metering_summary(
            path,
            tenant,
            pricing.resolve_baseline_model(overlay={}, store={}).model,
            AS_OF,
        )
    return path, tenant


def _cli(path: Path, *args: str) -> Result:
    return CliRunner().invoke(metering_command, ["--db", str(path), *args])


def _served(path: Path, tenant: str) -> list[dict[str, Any]]:
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=METERING, tenant_id=tenant))
    )
    assert result.ok, result.error
    return result.rows


def _served_row(
    path: Path, tenant: str, kind: str, start: str, baseline: str
) -> dict[str, Any]:
    matches = [
        r
        for r in _served(path, tenant)
        if (r["window_kind"], r["window_start"], r["baseline_model"])
        == (kind, start, baseline)
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _store_digest(path: Path) -> str:
    """Every file of the store, so a write through a journal is seen too."""
    digest = hashlib.sha256()
    for part in sorted(path.parent.glob(path.name + "*")):
        digest.update(part.name.encode())
        digest.update(part.read_bytes())
    return digest.hexdigest()


# -- AC2: every printed field is the served row's -----------------------------


@pytest.mark.parametrize(
    ("args", "kind", "start"),
    [((), "all", ""), (("--day", "2026-09-27"), "day", "2026-09-27")],
    ids=["all", "day"],
)
def test_every_printed_field_is_the_served_rows_field(
    tmp_path: Path, args: tuple[str, ...], kind: str, start: str
) -> None:
    path, tenant = _install(tmp_path)
    result = _cli(path, *args, "--json")
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    baseline = pricing.resolve_baseline_model(overlay={}, store={}).model
    served = _served_row(path, tenant, kind, start, baseline)
    summary = served["summary_json"]
    assert isinstance(summary, dict)

    assert "summary_json" not in printed
    # Nothing printed that the row does not carry, and nothing it carries left out.
    assert set(printed) == (set(served) - {"summary_json"}) | set(summary)
    for key, value in printed.items():
        expected = served[key] if key in served else summary[key]
        assert value == expected, key
    assert printed["tenant_id"] == tenant


def test_render_performs_no_fold_write_or_price_arithmetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _tenant = _install(tmp_path)
    original_refresh = sqlite_metering_summary.refresh_metering_summary
    calls: list[str] = []

    def spy(name: str, real: Any) -> Any:
        def wrapper(*a: Any, **k: Any) -> Any:
            calls.append(name)
            return real(*a, **k)

        return wrapper

    targets: list[tuple[Any, str]] = [
        (sqlite_metering_summary, "refresh_metering_summary"),
        (sqlite_metering_summary, "store_rows"),
        (handler_metering_summary_writer, "store_rows"),
        (HandlerProjectionMeteringSummary, "handle"),
        (handler_metering_summary.HandlerMeteringSummary, "handle"),
        (handler_metering_summary, "counterfactual_cost_usd"),
        (SqliteDatabaseAdapter, "upsert"),
        (pricing, "estimate_baseline_cost_usd"),
        (pricing, "estimate_baseline_savings_usd"),
    ]
    cli_module = __import__("omnimarket.cli.cli_metering", fromlist=["x"])
    if hasattr(cli_module, "refresh_metering_summary"):
        targets.append((cli_module, "refresh_metering_summary"))
    for owner, name in targets:
        monkeypatch.setattr(owner, name, spy(name, getattr(owner, name)))

    before = _store_digest(path)
    for args in ((), ("--json",), ("--window", "today", "--json")):
        _cli(path, *args)
    assert calls == []
    assert _store_digest(path) == before

    # Positive control: the same spies fire when the fold really runs.
    original_refresh(path, "control-tenant", "model-x", AS_OF)
    assert "handle" in calls
    assert "store_rows" in calls


# -- AC2: absent or stale rows are typed states, never recomputed -------------


def test_a_missing_row_is_a_typed_state_naming_the_repair(tmp_path: Path) -> None:
    path, tenant = _install(tmp_path, refresh=False)
    before = _store_digest(path)

    result = _cli(path, "--json")
    assert result.exit_code == 1, result.output
    state = json.loads(result.stdout)
    assert state["state"] == "METERING_SUMMARY_MISSING"
    assert state["tenant_id"] == tenant
    assert state["window_kind"] == "all"
    assert "onex delegate" in state["repair"]
    assert "savings_usd" not in state
    assert "runs_total" not in state

    text = _cli(path)
    assert text.exit_code == 1
    assert "METERING_SUMMARY_MISSING" in text.stderr
    assert "onex delegate" in text.stderr
    assert "Saved" not in text.output

    assert _store_digest(path) == before


def test_a_day_with_no_runs_says_so_instead_of_printing_zero(tmp_path: Path) -> None:
    path, _tenant = _install(tmp_path)
    result = _cli(path, "--day", "2026-01-01", "--json")
    assert result.exit_code == 1, result.output
    state = json.loads(result.stdout)
    assert state["state"] == "METERING_SUMMARY_MISSING"
    assert state["window_start"] == "2026-01-01"
    assert "no delegation" in state["reason"]


def test_a_row_older_than_a_recorded_run_is_stale_not_recomputed(
    tmp_path: Path,
) -> None:
    path, _tenant = _install(tmp_path)
    late = AS_OF + timedelta(hours=1)
    _seed_events(path, (("late", late, 10, 10, "0.01"),))
    before = _store_digest(path)

    result = _cli(path, "--json")
    assert result.exit_code == 1, result.output
    state = json.loads(result.stdout)
    assert state["state"] == "METERING_SUMMARY_STALE"
    assert state["row_as_of"] == AS_OF.isoformat()
    assert state["newest_run_at"] == late.isoformat()
    assert "onex delegate" in state["repair"]
    assert "savings_usd" not in state
    assert _store_digest(path) == before


def test_a_row_priced_against_another_manifest_version_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _tenant = _install(tmp_path)
    real = resolve_baseline

    def next_manifest(model: str) -> ModelCounterfactualBaseline | None:
        resolved = real(model)
        if resolved is None:
            return None
        return resolved.model_copy(update={"pricing_manifest_version": "9.9.9"})

    monkeypatch.setattr(
        "omnimarket.projection.sqlite_metering_summary.resolve_baseline", next_manifest
    )
    before = _store_digest(path)

    result = _cli(path, "--json")
    assert result.exit_code == 1, result.output
    state = json.loads(result.stdout)
    assert state["state"] == "METERING_SUMMARY_STALE"
    assert state["current_pricing_manifest_version"] == "9.9.9"
    assert state["row_pricing_manifest_version"] != "9.9.9"
    assert _store_digest(path) == before


def test_another_tenants_row_is_never_printed(tmp_path: Path) -> None:
    path, tenant = _install(tmp_path)
    printed = json.loads(_cli(path, "--json").stdout)
    assert printed["tenant_id"] == tenant
    assert printed["runs_total"] == len(RECORDS)
    assert Decimal(printed["spend_usd"]) == Decimal("0.2")


def test_include_fixtures_is_refused_rather_than_written_under_the_real_key(
    tmp_path: Path,
) -> None:
    path, _tenant = _install(tmp_path)
    before = _store_digest(path)
    result = _cli(path, "--include-fixtures", "--json")
    assert result.exit_code != 0
    assert "fixture" in result.output
    assert _store_digest(path) == before


# -- AC6 / AC7: the same row Overview selects ---------------------------------


def test_the_default_baseline_is_resolved_not_frozen_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution that changes after import is what the CLI reads.

    The ``--baseline`` default used to be ``DEFAULT_BASELINE_MODEL`` captured
    when the module loaded, so the CLI could select a different row from the
    one the delegation and the page resolve.
    """
    monkeypatch.setattr(pricing, "DEFAULT_BASELINE_MODEL", PRICED_BASELINE)
    path, tenant = _install(tmp_path)
    result = _cli(path, "--json")
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed["baseline_model"] == PRICED_BASELINE
    assert printed["window_kind"] == "all"
    assert printed["window_start"] == ""
    served = _served_row(path, tenant, "all", "", PRICED_BASELINE)
    assert printed["savings_usd"] == served["savings_usd"]


def test_an_unresolved_baseline_is_named_and_never_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pricing, "DEFAULT_BASELINE_MODEL", UNPRICED_BASELINE)
    path, _tenant = _install(tmp_path)

    result = _cli(path, "--json")
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed["baseline_model"] == UNPRICED_BASELINE
    assert printed["baseline_state"] == "unresolved"
    assert printed["savings_usd"] is None
    assert printed["counterfactual_usd"] is None
    assert printed["pricing_manifest_version"] is None
    assert "BASELINE_UNRESOLVED" in result.stderr

    text = _cli(path)
    assert text.exit_code == 0, text.output
    assert "BASELINE_UNRESOLVED" in text.stdout
    assert "Saved                unknown" in text.stdout
    assert "$0" not in next(
        line for line in text.stdout.splitlines() if line.startswith("Saved")
    )


def test_an_unmeasured_run_is_counted_and_in_no_sum(tmp_path: Path) -> None:
    path, _tenant = _install(tmp_path)
    printed = json.loads(_cli(path, "--json").stdout)
    assert printed["runs_total"] == 3
    assert printed["runs_measured"] == 1
    assert printed["runs_unknown_tokens"] == 1
    assert printed["runs_unknown_spend"] == 1
    # Only the measured run's tokens and spend are summed.
    assert printed["tokens_in"] == 1050
    assert Decimal(printed["spend_usd"]) == Decimal("0.2")

    text = _cli(path).stdout
    assert "in no sum" in text
