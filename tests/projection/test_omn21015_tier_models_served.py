# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC2 on the local store: the Tier mix lists each model under its tier with its share of runs and its cost.

Served through the real local dashboard app over a SQLite store, the path a laptop reads. Jonah's ruling on OMN-21015
(2026-10-14T04:50Z): the cost comes from the view, summed per tier and model, with an unmeasured count beside it.

Failure modes, each asserted below:
F1  two models in one tier render as one entry (the ticket's falsifier);
F2  a model's share uses another denominator than its tier's, so the shares do not sum to the tier's share;
F3  a group whose runs carry no cost serves 0 instead of null, or loses its unmeasured count;
F4  a measured zero cost reads as unmeasured;
F5  another tenant's runs enter this tenant's models;
F6  the keys the page already reads change.
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
_OTHER = "7c1e2b40-3a4f-4b6d-9e8a-112233445566"
_ROUTING = "onex.snapshot.projection.delegation.model-routing.v1"
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

# (tenant, tier, model, cost) per run. None is a run whose cost was not recorded.
_RUNS: list[tuple[str, str, str, float | None]] = [
    (_TENANT, "local", "qwen-a", 0.0),
    (_TENANT, "local", "qwen-a", 0.0),
    (_TENANT, "local", "qwen-a", 0.0),
    (_TENANT, "local", "qwen-b", None),
    (_TENANT, "cheap_cloud", "gemini", 0.004),
    (_TENANT, "cheap_cloud", "gemini", 0.005),
    (_TENANT, "", "unrouted-model", 0.0),
    (_OTHER, "local", "qwen-a", 0.0),
    (_OTHER, "local", "qwen-a", 0.0),
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(path)
    for index, (tenant, tier, model, cost) in enumerate(_RUNS):
        db.upsert(
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": f"omn21015-ac2-{index:02d}",
                "tenant_id": tenant,
                "task_type": "research",
                "delegated_to": model,
                "cost_tier_name": tier,
                "cost_usd": cost,
                "quality_gate_passed": True,
                "data_source": "real",
                "created_at": f"2026-10-14T05:{index:02d}:00+00:00",
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


def _tiers(client: TestClient) -> dict[str, dict[str, object]]:
    response = client.get(f"/projection/{_ROUTING}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    assert len(rows) == 1
    return {tier["cost_tier_name"]: tier for tier in rows[0]["by_tier"]["tiers"]}


def test_f1_two_models_in_one_tier_are_two_entries(client: TestClient) -> None:
    local = _tiers(client)["local"]
    assert [m["model_name"] for m in local["models"]] == ["qwen-a", "qwen-b"]
    assert [m["count"] for m in local["models"]] == [3, 1]


def test_f2_model_shares_sum_to_the_tier_share_on_its_denominator(
    client: TestClient,
) -> None:
    tiers = _tiers(client)
    for name in ("local", "cheap_cloud"):
        tier = tiers[name]
        shares = [m["pct_of_tier_routed"] for m in tier["models"]]
        assert sum(shares) == pytest.approx(tier["pct_of_tier_routed"])
    assert [m["pct_of_tier_routed"] for m in tiers["local"]["models"]] == pytest.approx(
        [3 / 6, 1 / 6]
    )
    unrouted = tiers["not_tier_routed"]
    assert [m["pct_of_tier_routed"] for m in unrouted["models"]] == [0]


def test_f3_unrecorded_cost_is_null_with_its_count(client: TestClient) -> None:
    local = {m["model_name"]: m for m in _tiers(client)["local"]["models"]}
    assert local["qwen-b"]["cost_usd"] is None
    assert local["qwen-b"]["unmeasured_cost_count"] == 1


def test_f4_a_measured_cost_is_summed_and_a_measured_zero_is_zero(
    client: TestClient,
) -> None:
    tiers = _tiers(client)
    gemini = tiers["cheap_cloud"]["models"][0]
    assert gemini["cost_usd"] == pytest.approx(0.009)
    assert gemini["unmeasured_cost_count"] == 0
    qwen_a = tiers["local"]["models"][0]
    assert qwen_a["cost_usd"] == 0
    assert qwen_a["unmeasured_cost_count"] == 0


def test_f5_another_tenant_never_enters_this_tenant(client: TestClient) -> None:
    assert _tiers(client)["local"]["models"][0]["count"] == 3


def test_f6_the_keys_the_page_already_reads_are_unchanged(client: TestClient) -> None:
    for tier in _tiers(client).values():
        assert {"cost_tier_name", "count", "tier_routed", "pct_of_tier_routed"} <= set(
            tier
        )
