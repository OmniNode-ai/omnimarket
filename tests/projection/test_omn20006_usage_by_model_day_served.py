# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20006: the Usage page's exposure serves measured cost per tenant.

Calls for two tenants are folded and stored in a local SQLite store the way the
local writer does, then read back through the projection read node exactly as
``onex dashboard`` serves them, with the exposures built from the node contracts
(no hand-built topic map), so the contract declaration is what is under test.

Failure modes each test is written against:

* the exposure is not tenant-scoped: the reading tenant gets another tenant's rows,
  and the page would have to filter in the browser;
* the contract leaves ``measured_cost_usd`` or ``unmeasured_call_count`` out of the
  served row, so the page cannot tell measured cost from an estimate;
* a key with no measured call is served as ``0`` instead of a typed null;
* an estimated call's cost leaks into the served measured cost.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    apply_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallEvent,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

USAGE = "onex.snapshot.projection.usage-by-model-day.v1"
TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"

# (tenant, call_id, model, timestamp, tokens in, tokens out, cost, usage_source)
CALLS: tuple[tuple[str, str, str, str, int, int, float, str], ...] = (
    (TENANT, "t1-a", "local-a", "2026-09-27T09:00:00Z", 100, 10, 0.25, "measured"),
    (TENANT, "t1-b", "local-a", "2026-09-27T10:00:00Z", 50, 5, 9.0, "estimated"),
    (TENANT, "t1-c", "local-b", "2026-09-28T10:00:00Z", 30, 3, 4.0, "unknown"),
    (OTHER_TENANT, "t2-a", "local-a", "2026-09-27T09:00:00Z", 7, 1, 2.0, "measured"),
)


def _store(tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    fold = HandlerProjectionUsageByModelDay()
    for tenant, call_id, model, stamp, tin, tout, cost, source in CALLS:
        event = ModelUsageCallEvent.model_validate(
            {
                "call_id": call_id,
                "model_name": model,
                "timestamp": stamp,
                "prompt_tokens": tin,
                "completion_tokens": tout,
                "estimated_cost_usd": cost,
                "usage_source": source,
                "tenant_id": tenant,
            }
        )
        apply_usage_call(fold.handle(event), adapter)
    return db_path


@cache
def _topics() -> dict[str, ProjectionTableConfig]:
    """The exposures exactly as the node contracts declare them."""
    return build_projection_topic_map()


def _read(db_path: Path, tenant: str) -> list[dict[str, Any]]:
    topics = _topics()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )
    response = client.get(f"/projection/{USAGE}")
    assert response.status_code == 200, response.json()
    rows: list[dict[str, Any]] = response.json()["rows"]
    return rows


def test_a_tenant_reads_only_its_own_usage_rows(tmp_path: Path) -> None:
    db_path = _store(tmp_path)
    mine = _read(db_path, TENANT)
    theirs = _read(db_path, OTHER_TENANT)
    assert {r["tenant_id"] for r in mine} == {TENANT}
    assert {(r["usage_day"], r["model_id"]) for r in mine} == {
        ("2026-09-27", "local-a"),
        ("2026-09-28", "local-b"),
    }
    assert [(r["tenant_id"], r["model_id"]) for r in theirs] == [
        (OTHER_TENANT, "local-a")
    ]


def test_the_served_row_carries_measured_cost_and_the_unmeasured_count(
    tmp_path: Path,
) -> None:
    rows = {(r["usage_day"], r["model_id"]): r for r in _read(_store(tmp_path), TENANT)}
    mixed = rows[("2026-09-27", "local-a")]
    assert Decimal(str(mixed["measured_cost_usd"])) == Decimal("0.25")
    assert mixed["unmeasured_call_count"] == 1
    assert Decimal(str(mixed["cost_usd"])) == Decimal("9.25")
    assert mixed["call_count"] == 2
    unmeasured_only = rows[("2026-09-28", "local-b")]
    assert unmeasured_only["measured_cost_usd"] is None
    assert unmeasured_only["unmeasured_call_count"] == 1
