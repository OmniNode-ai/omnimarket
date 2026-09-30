# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the runtime writer seam and its parameterized SQL replacement."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_metering_summary"
)


class SqlRecordingAdapter:
    """Run the shared SQL subset in SQLite, enforcing per-dispatch loop scope.

    This verifies SQL columns, parameters, conflict keys and replacements. It
    does not claim live Postgres validation.
    """

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        ddl = (
            (NODE / "migrations/0000_create_metering_summary.sql")
            .read_text()
            .replace("public.", "")
        )
        table = ddl[ddl.index("CREATE TABLE") : ddl.index(");") + 2]
        self.connection.execute(table)
        self.connection.execute(
            "CREATE UNIQUE INDEX metering_summary_key ON metering_summary (tenant_id, window_kind, window_start, baseline_model)"
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connects = 0
        self.closes = 0
        self.fail = False

    async def connect(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.connects += 1

    async def close(self) -> None:
        self.closes += 1
        self.loop = None

    async def execute(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        if self.fail:
            raise RuntimeError("database unavailable")
        rows = self.connection.execute(
            sql.replace("public.", ""),
            {str(i): value for i, value in enumerate(params, 1)},
        ).fetchall()
        return [{"tenant_id": row[0]} for row in rows]


def test_writer_upserts_on_two_dispatches_and_closes_after_failure() -> None:
    adapter = SqlRecordingAdapter()
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = adapter  # type: ignore[assignment]
    writer._standalone_bindings = None
    data = {
        "tenant_id": "local",
        "records": [],
        "baseline_model": "absent",
        "baseline": None,
        "as_of": "2026-09-28T12:00:00+00:00",
        "days": ["2026-09-27"],
    }
    try:
        assert writer.handle(data)["rows_upserted"] == 2
        data["as_of"] = "2026-09-28T13:00:00+00:00"
        assert writer.handle(data)["rows_upserted"] == 2
        expected = HandlerProjectionMeteringSummary().handle(
            ModelMeteringSummaryFoldRequest.model_validate(data)
        )
        adapter.connection.row_factory = sqlite3.Row
        actual = adapter.connection.execute(
            "SELECT * FROM main.metering_summary ORDER BY window_kind, window_start"
        ).fetchall()
        assert [dict(row) for row in actual] == [
            row.model_dump(mode="json") for row in expected.rows
        ]
        adapter.fail = True
        with pytest.raises(RuntimeError, match="database unavailable"):
            writer.handle(data)
        assert adapter.connects == adapter.closes == 3
    finally:
        adapter.connection.close()


def test_contract_routes_only_the_writer_and_owns_table() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["node_type"] == "reducer"
    assert [
        entry["handler"]["name"] for entry in contract["handler_routing"]["handlers"]
    ] == ["MeteringSummaryProjectionWriter"]
    assert issubclass(MeteringSummaryProjectionWriter, BaseProjectionRunner)
    assert contract["db_io"]["db_tables"][0]["name"] == "metering_summary"
