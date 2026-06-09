# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12853: per-topic multi-handler routing for the generation contract.

The node_generation_consumer contract subscribes to TWO command topics and now
declares TWO handler entries, each with a per-handler event_type:

  node-generation-requested -> HandlerGenerationConsumer (ModelNodeGenerationRequest)
  node-deploy               -> HandlerGeneratedExecutor  (ModelNodeDeploy)

This proves the runtime _topics_for_handler_entry routes each subscribe topic to
its declared handler (so node-deploy reaches the sandbox executor instead of
DLQ'ing), closing the self-extension loop's invoke leg. Regression guard for
defect #5 (OMN-12853): before this fix the contract had only the consumer entry,
so node-deploy was subscribed-but-unrouted ("No dispatcher found").
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _topics_for_handler_entry,
)
from omnibase_infra.runtime.auto_wiring.models import (
    ModelContractVersion,
    ModelDiscoveredContract,
    ModelEventBusWiring,
    ModelHandlerRef,
    ModelHandlerRouting,
    ModelHandlerRoutingEntry,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_generation_consumer"
    / "contract.yaml"
)

_TOPIC_GEN = "onex.cmd.omnimarket.node-generation-requested.v1"
_TOPIC_DEPLOY = "onex.cmd.omnimarket.node-deploy.v1"


def _load_contract_as_discovered() -> ModelDiscoveredContract:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text())
    hr = raw["handler_routing"]
    entries = tuple(
        ModelHandlerRoutingEntry(
            handler=ModelHandlerRef(
                name=h["handler"]["name"], module=h["handler"]["module"]
            ),
            event_model=ModelHandlerRef(
                name=h["event_model"]["name"], module=h["event_model"]["module"]
            ),
            event_type=h.get("event_type"),
            operation=h.get("operation"),
        )
        for h in hr["handlers"]
    )
    return ModelDiscoveredContract(
        name=raw["name"],
        node_type="ORCHESTRATOR_GENERIC",
        contract_version=ModelContractVersion(major=1, minor=0, patch=0),
        contract_path=_CONTRACT_PATH,
        entry_point_name=raw["name"],
        package_name="omnimarket",
        event_bus=ModelEventBusWiring(
            subscribe_topics=tuple(raw["event_bus"]["subscribe_topics"]),
            publish_topics=tuple(raw["event_bus"]["publish_topics"]),
        ),
        handler_routing=ModelHandlerRouting(
            routing_strategy=hr["routing_strategy"], handlers=entries
        ),
    )


@pytest.mark.integration
def test_production_contract_declares_both_handlers() -> None:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text())
    names = {h["handler"]["name"] for h in raw["handler_routing"]["handlers"]}
    assert "HandlerGenerationConsumer" in names
    assert "HandlerGeneratedExecutor" in names, (
        "node-deploy executor must be wired into handler_routing (OMN-12853)"
    )


@pytest.mark.integration
def test_each_subscribe_topic_routes_to_its_handler() -> None:
    contract = _load_contract_as_discovered()
    by_handler = {
        e.handler.name: _topics_for_handler_entry(contract, e)
        for e in contract.handler_routing.handlers
    }

    assert by_handler["HandlerGenerationConsumer"] == (_TOPIC_GEN,), (
        f"generation requests must route to the consumer; got {by_handler}"
    )
    assert by_handler["HandlerGeneratedExecutor"] == (_TOPIC_DEPLOY,), (
        f"node-deploy must route to the executor; got {by_handler}"
    )


@pytest.mark.integration
def test_executor_handler_module_is_importable_and_has_handle() -> None:
    """The wired executor must expose the dispatch entrypoint handle()."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generated_executor import (
        HandlerGeneratedExecutor,
    )

    assert callable(getattr(HandlerGeneratedExecutor, "handle", None)), (
        "HandlerGeneratedExecutor must expose handle() for runtime dispatch"
    )
