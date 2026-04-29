"""Contract tests for Task 11 cost projection snapshot nodes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path("src/omnimarket/nodes")

CASES = [
    (
        "node_projection_cost_summary",
        [
            "onex.evt.omniintelligence.llm-call-completed.v1",
            "onex.evt.omnibase-infra.savings-estimated.v1",
        ],
        "onex.snapshot.projection.cost.summary.v1",
        "local.omnibase_infra.node_projection_cost_summary.consume.v1",
        "ModelCostSummarySnapshot",
    ),
    (
        "node_projection_cost_by_repo",
        ["onex.evt.omniintelligence.llm-call-completed.v1"],
        "onex.snapshot.projection.cost.by_repo.v1",
        "local.omnibase_infra.node_projection_cost_by_repo.consume.v1",
        "ModelCostByRepoSnapshot",
    ),
    (
        "node_projection_cost_token_usage",
        ["onex.evt.omniintelligence.llm-call-completed.v1"],
        "onex.snapshot.projection.cost.token_usage.v1",
        "local.omnibase_infra.node_projection_cost_token_usage.consume.v1",
        "ModelCostTokenUsageSnapshot",
    ),
]


@pytest.mark.parametrize(
    ("node_name", "subscribe_topics", "publish_topic", "consumer_group", "model_name"),
    CASES,
)
def test_cost_projection_contract_declares_snapshot_topics(
    node_name: str,
    subscribe_topics: list[str],
    publish_topic: str,
    consumer_group: str,
    model_name: str,
) -> None:
    contract_path = ROOT / node_name / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())

    assert contract["name"] == node_name
    assert contract["node_type"] == "REDUCER_GENERIC"
    assert contract["event_bus"]["subscribe_topics"] == subscribe_topics
    assert contract["event_bus"]["publish_topics"] == [publish_topic]
    assert contract["event_bus"]["consumer_group"] == consumer_group
    assert contract["handler_routing"]["routing_strategy"] == "payload_type_match"
    assert contract["idempotency"] == {
        "enabled": True,
        "strategy": "snapshot_upsert",
        "hash_fields": ["window", "snapshot_timestamp_minute"],
    }
    assert any(
        route["output_model"].endswith(model_name)
        for route in contract["handler_routing"]["routes"]
    )
