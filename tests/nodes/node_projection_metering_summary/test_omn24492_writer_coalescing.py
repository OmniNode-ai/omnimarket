# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24492: the metering writer coalesces refreshes through its coalescer.

The refresh command seam (metering refresh -> MeteringSummaryProjectionWriter ->
store reads/upserts) now runs at most one day-refresh per tenant per interval.
These tests drive the writer's runtime entry point with a mutable fake clock and
the same fake store shape as the store-refresh tests; they do not claim live
Postgres validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.adapters.asyncpg_adapter import _refuse_unbound_tenant_scoped_write
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import METERING_COLUMNS
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    RECORDS_SQL,
    VISIBLE_TENANTS_SQL,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
    coalesce_default_seconds,
)
from omnimarket.nodes.node_projection_metering_summary.metering_refresh_coalescer import (
    MeteringRefreshCoalescer,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary"
)
_DEV = "820272f9-4aaf-5add-a2df-0af942852ab2"
_T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def _run(correlation: str, at: datetime, tokens: int = 100) -> dict[str, Any]:
    # The shape asyncpg returns: TIMESTAMPTZ as datetime, NUMERIC as Decimal. The
    # keys are the producer's own column list, so a column the reader adds or
    # drops breaks this fixture instead of leaving it green.
    return dict(
        zip(
            METERING_COLUMNS,
            (
                correlation,
                at,
                "qwen3.8-27b",
                "qwen3.8-27b",
                "document",
                tokens,
                tokens // 2,
                Decimal("0.0001"),
                Decimal("0.01"),
            ),
            strict=True,
        )
    )


class FakeStore:
    """Answers the refresh's reads by statement and keeps the upserted rows."""

    def __init__(
        self, runs: dict[str, list[dict[str, Any]]], fail_records: bool = False
    ) -> None:
        self.runs = runs
        # What a role that bypasses row-level security sees with no tenant set.
        self.visible = [_DEV]
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.reads: list[tuple[str, str | None]] = []
        self.fail_records = fail_records

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
            if self.fail_records:
                self.fail_records = False
                raise RuntimeError("records read failed")
            # Row-level security: only the GUC's tenant is visible.
            assert params == (tenant,)
            return list(self.runs.get(str(tenant), []))
        if sql.startswith("SELECT pricing_manifest_version"):
            self.reads.append(("stored", tenant))
            key = (str(params[0]), "all", "", str(params[1]))
            row = self.rows.get(key)
            return [] if row is None else [row]
        if sql.startswith("INSERT INTO public.metering_summary"):
            _refuse_unbound_tenant_scoped_write(sql, tenant)
            columns = sql[sql.index("(") + 1 : sql.index(")")].split(", ")
            row = dict(zip(columns, params, strict=True))
            assert tenant == row["tenant_id"]
            key = (
                row["tenant_id"],
                row["window_kind"],
                row["window_start"],
                row["baseline_model"],
            )
            self.rows[key] = row
            return [{"tenant_id": row["tenant_id"]}]
        raise AssertionError(f"unexpected statement: {sql}")


def _writer(
    store: FakeStore,
    *,
    coalescer: MeteringRefreshCoalescer | None = None,
    clock: Any | None = None,
) -> MeteringSummaryProjectionWriter:
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = cast(Any, store)
    writer._standalone_bindings = None
    writer._coalescer = coalescer
    writer._clock = clock
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


def _terminal(
    i: int,
    day: str,
    clock: Any,
    store: FakeStore,
    writer: MeteringSummaryProjectionWriter,
) -> dict[str, Any]:
    command = _command(
        reason="delegation_terminal",
        refresh_id=f"delegation-terminal-{i}",
        tenant_ids=[_DEV],
        days=[day],
    )
    return writer.handle({**command, "_topic": _store_topic()})


def test_golden_a_burst_of_terminals_reads_the_store_once() -> None:
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(
        store, coalescer=MeteringRefreshCoalescer(timedelta(seconds=300)), clock=clock
    )
    results = []
    for i in range(10):
        results.append(_terminal(i, "2026-10-19", clock, store, writer))
        now += timedelta(seconds=1)

    assert store.reads.count(("records", _DEV)) == 1
    assert results[0]["refresh_coalesced"] is False
    assert results[0]["rows_upserted"] > 0
    for result in results[1:]:
        assert result["refresh_coalesced"] is True
        assert result["rows_upserted"] == 0
    assert results[-1]["refresh_coalesced_total"] == 9
    assert results[-1]["refresh_runs_total"] == 1
    assert any(k[0] == _DEV and k[1] == "day" for k in store.rows)


def test_control_without_an_interval_every_terminal_reads_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(store, clock=clock)
    results = []
    for i in range(10):
        results.append(_terminal(i, "2026-10-19", clock, store, writer))
        now += timedelta(seconds=1)

    assert store.reads.count(("records", _DEV)) == 10
    assert all(result["refresh_coalesced"] is False for result in results)


def test_golden_held_back_days_are_carried_by_the_next_run() -> None:
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(
        store, coalescer=MeteringRefreshCoalescer(timedelta(seconds=300)), clock=clock
    )
    _terminal(0, "2026-10-19", clock, store, writer)
    now += timedelta(seconds=10)
    held = _terminal(1, "2026-10-20", clock, store, writer)
    assert held["refresh_coalesced"] is True
    assert held["refresh_pending_tenants"] == [_DEV]
    now += timedelta(seconds=300)
    last = _terminal(2, "2026-10-19", clock, store, writer)
    assert last["refresh_coalesced"] is False
    assert last["refresh_pending_tenants"] == []
    day_rows = [k for k in store.rows if k[0] == _DEV and k[1] == "day"]
    assert any(k[2] == "2026-10-20" for k in day_rows)


def test_golden_a_schedule_refresh_inside_the_interval_still_runs_and_settles_pending() -> (
    None
):
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(
        store, coalescer=MeteringRefreshCoalescer(timedelta(seconds=300)), clock=clock
    )
    _terminal(0, "2026-10-19", clock, store, writer)
    now += timedelta(seconds=10)
    held = _terminal(1, "2026-10-20", clock, store, writer)
    assert held["refresh_coalesced"] is True
    now += timedelta(seconds=10)
    result = writer.handle({**_command(), "_topic": _store_topic()})
    assert result["refresh_coalesced"] is False
    assert result["refresh_pending_tenants"] == []
    assert ("visible", None) in store.reads


def test_error_a_failed_run_is_retried_at_once_and_loses_no_day() -> None:
    store = FakeStore(
        {_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]},
        fail_records=True,
    )
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(
        store, coalescer=MeteringRefreshCoalescer(timedelta(seconds=300)), clock=clock
    )
    with pytest.raises(RuntimeError):
        _terminal(0, "2026-10-19", clock, store, writer)
    now += timedelta(seconds=1)
    result = _terminal(1, "2026-10-19", clock, store, writer)
    assert result["refresh_coalesced"] is False
    assert result["rows_upserted"] > 0


def test_golden_the_overlay_sets_the_interval(tmp_path: Path) -> None:
    overlay = tmp_path / "node_projection_metering_summary" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("min_interval_seconds: 300\n", encoding="utf-8")
    store = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(store, clock=clock)
    import os

    os.environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    try:
        first = _terminal(0, "2026-10-19", clock, store, writer)
        assert first["refresh_coalesced"] is False
        now += timedelta(seconds=10)
        held = _terminal(1, "2026-10-20", clock, store, writer)
        assert held["refresh_coalesced"] is True
    finally:
        del os.environ["ONEX_SKILL_OVERLAY_ROOTS"]

    overlay.write_text("min_interval_seconds: 0\n", encoding="utf-8")
    store2 = FakeStore({_DEV: [_run("a", _T0), _run("b", _T0 + timedelta(days=1))]})
    now2 = _T0

    def clock2() -> datetime:
        return now2

    writer2 = _writer(store2, clock=clock2)
    os.environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    try:
        first = _terminal(0, "2026-10-19", clock2, store2, writer2)
        assert first["refresh_coalesced"] is False
        now2 += timedelta(seconds=10)
        second = _terminal(1, "2026-10-20", clock2, store2, writer2)
        assert second["refresh_coalesced"] is False
    finally:
        del os.environ["ONEX_SKILL_OVERLAY_ROOTS"]


def test_error_a_broken_overlay_refuses_the_refresh(tmp_path: Path) -> None:
    overlay = tmp_path / "node_projection_metering_summary" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("min_interval_seconds: -1\n", encoding="utf-8")
    store = FakeStore({_DEV: [_run("a", _T0)]})
    now = _T0

    def clock() -> datetime:
        return now

    writer = _writer(store, clock=clock)
    import os

    os.environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    try:
        with pytest.raises(NodeOverlayError):
            _terminal(0, "2026-10-19", clock, store, writer)
    finally:
        del os.environ["ONEX_SKILL_OVERLAY_ROOTS"]
    assert store.reads == []


def test_golden_the_contract_default_is_zero_and_names_the_overlay_field() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert (
        contract["config"]["metering_summary"]["refresh_coalesce"][
            "default_min_interval_seconds"
        ]
        == 0
    )
    assert coalesce_default_seconds() == 0
