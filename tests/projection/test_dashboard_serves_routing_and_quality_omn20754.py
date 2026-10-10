# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard`` lists and serves the Overview's routing and quality topics (OMN-20754).

The served Overview's Run locally and Tier mix rows read
``delegation.model-routing.v1`` and its Quality row reads
``delegation.quality-gate.v1``. On a local install the dashboard listed both
``degraded`` (``not_in_local_store``), so the page showed those rows as Not
served. This is the server half of that page: the store a fresh install has
after one recorded delegation, the dashboard's own catalogue and reads, and
the fields the three Overview rows take from each row.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
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
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_ROUTING = "onex.snapshot.projection.delegation.model-routing.v1"
_QUALITY = "onex.snapshot.projection.delegation.quality-gate.v1"

# node_projection_delegation's own exposures, parsed by the topic map's section
# parser: discovering every contract takes seconds, and dod-verify gives this
# file 30.
_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_TOPICS = {
    cfg.topic: cfg
    for cfg in load_projection_exposures_from_contract(
        yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
        "node_projection_delegation",
        _CONTRACT,
    )
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(path).upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": "b4030008-5873-4569-8ecc-98cd7247f751",
            "tenant_id": _TENANT,
            "task_type": "reasoning",
            "delegated_to": "qwen-local",
            "cost_tier_name": "local",
            "quality_gate_passed": True,
            "actual_score": 0.9,
            "required_bar": 0.7,
            "latency_ms": 938,
            "data_source": "real",
            "created_at": "2026-10-08T03:11:00+00:00",
        },
    )
    source = SqliteTableRowSource(path)
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=_TOPICS, row_source=source),
            tenant=_TENANT,
            topic_map=_TOPICS,
            row_source=source,
        )
    )


def _listed(client: TestClient, topic: str) -> dict[str, object]:
    rows = client.get("/projections").json()["topics"]
    row: dict[str, object] = next(row for row in rows if row["topic"] == topic)
    return row


def _served(client: TestClient, topic: str) -> dict[str, object]:
    response = client.get(f"/projection/{topic}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    assert len(rows) == 1
    row: dict[str, object] = rows[0]
    return row


@pytest.mark.parametrize("topic", [_ROUTING, _QUALITY])
def test_the_catalogue_lists_the_topic_ok_and_bus_backed(
    client: TestClient, topic: str
) -> None:
    row = _listed(client, topic)
    assert (row["status"], row["backing"]) == ("ok", "bus")


def test_run_locally_and_tier_mix_have_their_served_fields(client: TestClient) -> None:
    by_tier = _served(client, _ROUTING)["by_tier"]
    assert isinstance(by_tier, dict)
    assert by_tier["local_call_share"] == 1.0
    assert by_tier["tier_routed_total"] == 1
    assert by_tier["not_tier_routed_count"] == 0
    assert by_tier["tiers"] == [
        {
            "cost_tier_name": "local",
            "count": 1,
            "tier_routed": True,
            "pct_of_tier_routed": 1.0,
        }
    ]


def test_quality_has_every_field_the_panel_shows(client: TestClient) -> None:
    row = _served(client, _QUALITY)
    # The Overview's QUALITY_FIELDS, each read as served.
    assert {
        field: row[field]
        for field in (
            "overall_pass_rate",
            "total_passed",
            "total_failed",
            "total_checks",
            "avg_actual_score",
            "avg_required_bar",
        )
    } == {
        "overall_pass_rate": 1.0,
        "total_passed": 1,
        "total_failed": 0,
        "total_checks": 1,
        "avg_actual_score": 0.9,
        "avg_required_bar": 0.7,
    }
