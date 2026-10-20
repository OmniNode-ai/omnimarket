# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the metering writer refreshes a tenant from its stored runs.

The shared lanes' refresh command names tenants and days, never runs, because
a tenant's runs do not fit one bus message. The writer reads each tenant's
delegation_events under that tenant's row-level-security context, folds them
through the same handler as a fold command, and upserts the rows through the
same statement. These tests run the SQL against an in-memory fake store; they
do not claim live Postgres validation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import (
    ModelMeteringRecord,
    metering_record_from_row,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    RECORDS_SQL,
    VISIBLE_TENANTS_SQL,
    plan_tenant_fold,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary_topic,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary"
)
_DEV = "820272f9-4aaf-5add-a2df-0af942852ab2"
_OTHER = "11111111-2222-5333-8444-555555555555"
_EMPTY = "99999999-2222-5333-8444-555555555555"
_T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def _run(correlation: str, at: datetime, tokens: int = 100) -> dict[str, Any]:
    # The shape asyncpg returns: TIMESTAMPTZ as datetime, NUMERIC as Decimal.
    return {
        "correlation_id": correlation,
        "created_at": at,
        "delegated_to": "qwen3.8-27b",
        "model_name": "qwen3.8-27b",
        "task_type": "document",
        "tokens_input": tokens,
        "tokens_output": tokens // 2,
        "cost_usd": Decimal("0.0001"),
        "cost_savings_usd": Decimal("0.01"),
    }


class FakeStore:
    """Answers the refresh's reads by statement and keeps the upserted rows."""

    def __init__(self, runs: dict[str, list[dict[str, Any]]]) -> None:
        self.runs = runs
        # What a role that bypasses row-level security sees with no tenant set.
        self.visible = [_DEV, _OTHER, _EMPTY]
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.reads: list[tuple[str, str | None]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(
        self, sql: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        if sql == VISIBLE_TENANTS_SQL:
            self.reads.append(("visible", tenant))
            return [{"tenant_id": t} for t in self.visible]
        if sql == RECORDS_SQL:
            self.reads.append(("records", tenant))
            # Row-level security: only the GUC's tenant is visible.
            assert params == (tenant,)
            return list(self.runs.get(str(tenant), []))
        if sql.startswith("SELECT pricing_manifest_version"):
            self.reads.append(("stored", tenant))
            key = (str(params[0]), "all", "", str(params[1]))
            row = self.rows.get(key)
            return [] if row is None else [row]
        if sql.startswith("INSERT INTO public.metering_summary"):
            columns = sql[sql.index("(") + 1 : sql.index(")")].split(", ")
            row = dict(zip(columns, params, strict=True))
            key = (
                row["tenant_id"],
                row["window_kind"],
                row["window_start"],
                row["baseline_model"],
            )
            self.rows[key] = row
            return [{"tenant_id": row["tenant_id"]}]
        raise AssertionError(f"unexpected statement: {sql}")


def _writer(store: FakeStore) -> MeteringSummaryProjectionWriter:
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = cast(Any, store)
    writer._standalone_bindings = None
    return writer


def _command(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "refresh_id": "metering-summary-refresh-20261019T080000Z",
        "reason": "schedule",
        "tenant_ids": None,
        "days": None,
        "requested_at": "2026-10-19T08:00:00Z",
    }
    body.update(overrides)
    return ModelMeteringSummaryRefreshRequested.model_validate(body).model_dump(
        mode="json"
    )


def _store_topic() -> str:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    return str(contract["config"]["metering_summary"]["store_refresh_command_topic"])


def test_every_tenant_with_runs_gets_day_and_all_rows() -> None:
    store = FakeStore(
        {
            _DEV: [
                _run("a", _T0),
                _run("b", _T0 + timedelta(days=1)),
            ],
            _OTHER: [_run("c", _T0)],
        }
    )
    result = _writer(store).handle({**_command(), "_topic": _store_topic()})

    assert result["tenants_refreshed"] == sorted([_DEV, _OTHER])
    assert result["tenants_without_runs"] == [_EMPTY]
    kinds = sorted((k[0], k[1], k[2]) for k in store.rows)
    assert kinds == sorted(
        [
            (_DEV, "all", ""),
            (_DEV, "day", "2026-10-18"),
            (_DEV, "day", "2026-10-19"),
            (_OTHER, "all", ""),
            (_OTHER, "day", "2026-10-18"),
        ]
    )
    dev_all = next(r for k, r in store.rows.items() if k[0] == _DEV and k[1] == "all")
    assert dev_all["runs_total"] == 2
    assert dev_all["tokens_in"] == 200
    # as_of is one microsecond past the newest run, never the wall clock.
    assert dev_all["as_of"] == (_T0 + timedelta(days=1, microseconds=1)).isoformat()
    # Every run read happened under its own tenant's RLS context.
    assert ("records", _DEV) in store.reads
    assert ("records", _OTHER) in store.reads


def test_named_tenant_and_day_refolds_that_day_once_the_all_row_exists() -> None:
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    writer = _writer(store)
    writer.handle({**_command(), "_topic": _store_topic()})
    store.runs[_DEV].append(_run("c", _T0 + timedelta(days=1, hours=1)))
    before = dict(store.rows)

    result = writer.handle(
        {
            **_command(
                refresh_id="delegation-terminal-c",
                reason="delegation_terminal",
                tenant_ids=[_DEV],
                days=["2026-10-19"],
            ),
            "_topic": _store_topic(),
        }
    )

    assert result["tenants_refreshed"] == [_DEV]
    # A named tenant is read directly; no tenant walk.
    assert ("visible", None) not in store.reads[-3:]
    day = store.rows[(_DEV, "day", "2026-10-19", dev_baseline(store))]
    assert day["runs_total"] == 2
    # The earlier day was not rewritten: its row is the same object.
    key = (_DEV, "day", "2026-10-18", dev_baseline(store))
    assert store.rows[key] is before[key]


def dev_baseline(store: FakeStore) -> str:
    return next(k[3] for k in store.rows if k[0] == _DEV)


def test_a_tenant_with_no_stored_all_row_is_refolded_whole() -> None:
    records = (
        _record("a", _T0),
        _record("b", _T0 + timedelta(days=1)),
    )
    plan = plan_tenant_fold(
        tenant_id=_DEV,
        records=records,
        days=frozenset({date(2026, 10, 19)}),
        baseline_model="claude-sonnet-4-5",
        baseline=None,
        stored_all_manifest_version=None,
        stored_all_present=False,
    )
    assert plan is not None
    assert plan.days is None
    assert plan.as_of == _T0 + timedelta(days=1, microseconds=1)


def test_a_manifest_change_refolds_the_tenant_whole() -> None:
    plan = plan_tenant_fold(
        tenant_id=_DEV,
        records=(_record("a", _T0),),
        days=frozenset({date(2026, 10, 18)}),
        baseline_model="claude-sonnet-4-5",
        baseline=None,
        stored_all_manifest_version="7",
        stored_all_present=True,
    )
    assert plan is not None
    assert plan.days is None


def test_no_runs_plans_nothing() -> None:
    assert (
        plan_tenant_fold(
            tenant_id=_EMPTY,
            records=(),
            days=None,
            baseline_model="claude-sonnet-4-5",
            baseline=None,
            stored_all_manifest_version=None,
            stored_all_present=False,
        )
        is None
    )


def test_fixture_runs_are_never_read() -> None:
    # OMN-19970: the dev seed's rows are never in a stored figure.
    assert "data_source <> 'fixture'" in RECORDS_SQL
    assert "FROM public.delegation_events" in RECORDS_SQL


def test_a_postgres_timestamp_is_a_run_not_a_dropped_row() -> None:
    record = metering_record_from_row(_run("a", _T0))
    assert record is not None
    assert record.occurred_at == _T0
    assert record.tokens_in == 100


def test_fold_command_still_folds_the_snapshot_it_carries() -> None:
    store = FakeStore({})
    record = _record("a", _T0)
    result = _writer(store).handle(
        {
            "_topic": refresh_metering_summary_topic(),
            "tenant_id": _DEV,
            "records": [record.model_dump(mode="json")],
            "baseline_model": "claude-sonnet-4-5",
            "as_of": (_T0 + timedelta(seconds=1)).isoformat(),
        }
    )
    assert result["rows_upserted"] == 2


def test_contract_declares_both_commands_and_the_reads() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    cfg = contract["config"]["metering_summary"]
    assert contract["event_bus"]["subscribe_topics"] == [
        cfg["fold_command_topic"],
        cfg["store_refresh_command_topic"],
    ]
    assert refresh_metering_summary_topic() == cfg["fold_command_topic"]
    tables = {t["name"]: t for t in contract["db_io"]["db_tables"]}
    assert tables["delegation_events"]["access"] == "read"
    # One in-process pool holds one identity: no cross-schema registry read.
    assert "tenant_registry_mirror" not in tables
    assert tables["metering_summary"]["access"] == "read_write"


def _record(correlation: str, at: datetime) -> ModelMeteringRecord:
    record = metering_record_from_row(_run(correlation, at))
    assert record is not None
    return record


def test_reason_enum_round_trips() -> None:
    body = _command(reason="backfill")
    assert body["reason"] == EnumMeteringRefreshReason.BACKFILL.value
