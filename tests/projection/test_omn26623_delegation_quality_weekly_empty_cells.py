# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The weekly delegation-quality view serves an empty row for a tenant with no attempt history (OMN-26623, AC6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from tests.projection.test_omn26623_delegation_quality_weekly import (
    _TENANT,
    _WEEKLY,
    _recorded,
    _store,
    _topics,
)

pytestmark = pytest.mark.unit


def _served_rows(path: Path, tenant: str) -> list[dict[str, Any]]:
    handler = HandlerProjectionRead(
        topic_map=_topics(), row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(
            ModelProjectionReadRequest(topic=_WEEKLY, tenant_id=tenant, limit=500)
        )
    )
    assert result.http_status == 200, result.response
    return list((result.response or {})["rows"])


def _cells_of(row: dict[str, Any]) -> list[Any]:
    cells = row["cells"]
    if isinstance(cells, str):
        cells = json.loads(cells)
    return list(cells)


def test_ac6_a_tenant_with_a_row_and_no_attempt_history_gets_one_row_with_empty_cells(
    tmp_path: Path,
) -> None:
    """AC6: one delegation_events row with an empty attempt history serves exactly one weekly row, and its cells are empty."""
    row = next(
        r
        for r in _recorded()
        if r["data_source"] != "experiment" and not r["attempt_history"]
    )
    path = _store(tmp_path, [row])
    rows = _served_rows(path, _TENANT)
    assert len(rows) == 1
    assert _cells_of(rows[0]) == []
    assert rows[0]["total_runs"] == 0


def test_ac6_a_tenant_with_runs_and_a_row_without_history_still_serves_its_cells(
    tmp_path: Path,
) -> None:
    """AC6: a tenant that has runs keeps its cells when another row has no attempt history."""
    rows_in = _recorded()
    assert any(not r["attempt_history"] for r in rows_in)
    assert any(r["attempt_history"] for r in rows_in)
    path = _store(tmp_path, rows_in)
    rows = _served_rows(path, _TENANT)
    assert len(rows) == 1
    assert len(_cells_of(rows[0])) > 0


def test_ac6_a_tenant_with_no_delegation_events_row_gets_no_row(tmp_path: Path) -> None:
    """AC6: a tenant with no delegation_events row at all is served no weekly row."""
    row = next(
        r
        for r in _recorded()
        if r["data_source"] != "experiment" and not r["attempt_history"]
    )
    path = _store(tmp_path, [row])
    assert _served_rows(path, "11111111-2222-4333-8444-555555555555") == []
