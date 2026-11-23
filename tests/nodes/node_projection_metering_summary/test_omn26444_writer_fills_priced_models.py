# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26444 (FILL-26444): the shared metering refresh fills savings_priced_models
from the pricing manifest through the writer's own adapter, with no tenant,
before it reads any run, so the shared savings view and the refresh read one
manifest; a failed fill stops the refresh before anything is folded."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from omnimarket.models.model_local_install_schema import local_priced_models_rows
from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    RECORDS_SQL,
    VISIBLE_TENANTS_SQL,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)

_TENANT = "a6666666-6666-4666-8666-666666666666"


class FakeStore:
    def __init__(self, *, fail_fill: bool = False) -> None:
        self.statements: list[tuple[str, tuple[Any, ...], str | None]] = []
        self.fail_fill = fail_fill

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(
        self, sql: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        self.statements.append((sql, params, tenant))
        if "savings_priced_models" in sql:
            if self.fail_fill:
                raise RuntimeError("fill failed")
            return []
        if sql == VISIBLE_TENANTS_SQL:
            return [{"tenant_id": _TENANT}]
        if sql == RECORDS_SQL:
            return []
        if sql.startswith("SELECT pricing_manifest_version"):
            return []
        raise AssertionError(f"unexpected statement: {sql}")


def _writer(store: FakeStore) -> MeteringSummaryProjectionWriter:
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = cast(Any, store)
    writer._standalone_bindings = None
    writer._coalescer = None
    writer._clock = None
    return writer


def _request() -> ModelMeteringSummaryRefreshRequested:
    return ModelMeteringSummaryRefreshRequested.model_validate(
        {
            "refresh_id": "metering-summary-refresh-20261123T030000Z",
            "reason": "schedule",
            "tenant_ids": None,
            "days": None,
            "requested_at": "2026-11-23T03:00:00Z",
        }
    )


def _fills(store: FakeStore) -> list[tuple[str, tuple[Any, ...], str | None]]:
    return [s for s in store.statements if "savings_priced_models" in s[0]]


def test_the_refresh_fills_the_priced_models_once_with_no_tenant() -> None:
    store = FakeStore()
    writer = _writer(store)
    asyncio.run(writer._refresh(_request()))
    fills = _fills(store)
    assert len(fills) == 1
    sql, params, tenant = fills[0]
    assert tenant is None
    assert "tenant_id" not in sql
    rows = local_priced_models_rows()
    assert list(params[0]) == [key for key, _ in rows]
    assert list(params[1]) == [bool(free) for _, free in rows]
    assert rows, "positive control: the manifest names at least one model"


def test_the_fill_runs_before_any_run_is_read() -> None:
    store = FakeStore()
    writer = _writer(store)
    asyncio.run(writer._refresh(_request()))
    first_fill = next(
        i for i, s in enumerate(store.statements) if "savings_priced_models" in s[0]
    )
    first_records = next(
        i
        for i, s in enumerate(store.statements)
        if s[0] in (RECORDS_SQL, VISIBLE_TENANTS_SQL)
    )
    assert first_fill < first_records


def test_a_failed_fill_stops_the_refresh_before_anything_is_read() -> None:
    store = FakeStore(fail_fill=True)
    writer = _writer(store)
    with pytest.raises(RuntimeError, match="fill failed"):
        asyncio.run(writer._refresh(_request()))
    assert not any(s[0] in (RECORDS_SQL, VISIBLE_TENANTS_SQL) for s in store.statements)
