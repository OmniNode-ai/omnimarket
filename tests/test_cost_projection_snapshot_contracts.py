"""Contract tests for Task 11 cost projection snapshot nodes."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

ROOT = Path("src/omnimarket/nodes")

CASES = [
    (
        "node_projection_cost_summary",
        "omnimarket.nodes.node_projection_cost_summary.handlers.handler_projection_cost_summary",
        "HandlerProjectionCostSummary",
        [
            "onex.evt.omniintelligence.llm-call-completed.v1",
            "onex.evt.omnibase-infra.savings-estimated.v1",
        ],
        "onex.evt.omnimarket.cost-summary-snapshot.v1",
        "local.omnibase_infra.node_projection_cost_summary.consume.v1",
        "ModelCostSummarySnapshot",
    ),
    (
        "node_projection_cost_by_repo",
        "omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_projection_cost_by_repo",
        "HandlerProjectionCostByRepo",
        ["onex.evt.omniintelligence.llm-call-completed.v1"],
        "onex.evt.omnimarket.cost-by-repo-snapshot.v1",
        "local.omnibase_infra.node_projection_cost_by_repo.consume.v1",
        "ModelCostByRepoSnapshot",
    ),
    (
        "node_projection_cost_token_usage",
        "omnimarket.nodes.node_projection_cost_token_usage.handlers.handler_projection_cost_token_usage",
        "HandlerProjectionCostTokenUsage",
        ["onex.evt.omniintelligence.llm-call-completed.v1"],
        "onex.evt.omnimarket.cost-token-usage-snapshot.v1",
        "local.omnibase_infra.node_projection_cost_token_usage.consume.v1",
        "ModelCostTokenUsageSnapshot",
    ),
]


@pytest.mark.parametrize(
    (
        "node_name",
        "handler_module",
        "handler_class",
        "subscribe_topics",
        "publish_topic",
        "consumer_group",
        "model_name",
    ),
    CASES,
)
def test_cost_projection_contract_declares_snapshot_topics(
    node_name: str,
    handler_module: str,
    handler_class: str,
    subscribe_topics: list[str],
    publish_topic: str,
    consumer_group: str,
    model_name: str,
) -> None:
    contract_path = ROOT / node_name / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())

    assert contract["name"] == node_name
    assert contract["node_type"] == "REDUCER_GENERIC"
    assert contract["handler"]["module"] == handler_module
    assert contract["handler"]["class"] == handler_class
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


@pytest.mark.parametrize(
    (
        "node_name",
        "handler_module",
        "handler_class",
        "_subscribe_topics",
        "_publish_topic",
        "_consumer_group",
        "_model_name",
    ),
    CASES,
)
def test_cost_projection_nodes_are_discoverable(
    node_name: str,
    handler_module: str,
    handler_class: str,
    _subscribe_topics: list[str],
    _publish_topic: str,
    _consumer_group: str,
    _model_name: str,
) -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}
    assert node_name in eps
    assert eps[node_name].value == f"omnimarket.nodes.{node_name}"
    assert eps[node_name].load().__name__ == f"omnimarket.nodes.{node_name}"

    handler = getattr(import_module(handler_module), handler_class)
    result = handler().handle({"timestamp": "2026-04-29T12:34:56Z"})
    assert result["snapshot_timestamp_minute"] == "2026-04-29T12:34:00+00:00"
