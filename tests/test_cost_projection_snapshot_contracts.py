"""Contract tests for Task 11 cost projection snapshot nodes."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

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


@pytest.mark.parametrize(
    (
        "node_name",
        "handler_module",
        "_handler_class",
        "_subscribe_topics",
        "_publish_topic",
        "_consumer_group",
        "_model_name",
    ),
    CASES,
)
def test_cost_projection_topic_constants_are_contract_derived(
    node_name: str,
    handler_module: str,
    _handler_class: str,
    _subscribe_topics: list[str],
    _publish_topic: str,
    _consumer_group: str,
    _model_name: str,
) -> None:
    contract = yaml.safe_load((ROOT / node_name / "contract.yaml").read_text())
    module = import_module(handler_module)

    assert tuple(contract["event_bus"]["subscribe_topics"]) == module.SUBSCRIBE_TOPICS
    assert tuple(contract["event_bus"]["publish_topics"]) == module.PUBLISH_TOPICS

    handler_source = Path(module.__file__).read_text(encoding="utf-8")
    assert "onex.evt." not in handler_source


def test_cost_projection_summary_preserves_explicit_zero_fallback_values() -> None:
    from omnimarket.nodes.node_projection_cost_summary.handlers.handler_projection_cost_summary import (
        HandlerProjectionCostSummary,
    )

    result = HandlerProjectionCostSummary().handle(
        {
            "timestamp": "2026-04-29T12:34:56Z",
            "estimated_cost_usd": 0,
            "total_cost_usd": "9.99",
            "savings_usd": 0,
            "savingsUsd": "8.88",
        }
    )

    assert result["total_estimated_cost_usd"] == "0"
    assert result["total_savings_usd"] == "0"


def test_cost_projection_by_repo_preserves_explicit_zero_fallback_values() -> None:
    from omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_projection_cost_by_repo import (
        HandlerProjectionCostByRepo,
    )

    result = HandlerProjectionCostByRepo().handle(
        {
            "timestamp": "2026-04-29T12:34:56Z",
            "estimated_cost_usd": 0,
            "total_cost_usd": "9.99",
            "total_tokens": 0,
            "totalTokens": 999,
        }
    )

    repo: dict[str, Any] = result["repositories"][0]
    assert repo["estimated_cost_usd"] == "0"
    assert repo["total_tokens"] == 0


def test_cost_projection_token_usage_preserves_explicit_total_zero() -> None:
    from omnimarket.nodes.node_projection_cost_token_usage.handlers.handler_projection_cost_token_usage import (
        HandlerProjectionCostTokenUsage,
    )

    result = HandlerProjectionCostTokenUsage().handle(
        {
            "timestamp": "2026-04-29T12:34:56Z",
            "prompt_tokens": 0,
            "promptTokens": 10,
            "completion_tokens": 0,
            "completionTokens": 5,
            "total_tokens": 0,
            "totalTokens": 15,
        }
    )

    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0
    assert result["total_tokens"] == 0


def test_cost_projection_token_usage_derives_total_when_absent() -> None:
    from omnimarket.nodes.node_projection_cost_token_usage.handlers.handler_projection_cost_token_usage import (
        HandlerProjectionCostTokenUsage,
    )

    result = HandlerProjectionCostTokenUsage().handle(
        {
            "timestamp": "2026-04-29T12:34:56Z",
            "prompt_tokens": 2,
            "completion_tokens": 3,
        }
    )

    assert result["total_tokens"] == 5
