# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical handler-routing contracts preserve their dispatch targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_handler_routing_subcontract import (
    ModelHandlerRoutingSubcontract,
)

_NODES_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_MIGRATED_ROUTES: dict[str, dict[str, str]] = {
    "node_agent_coordinator_orchestrator": {
        "subscribe": "HandlerSubscription",
        "unsubscribe": "HandlerSubscription",
        "list_subscriptions": "HandlerSubscription",
        "notify": "HandlerSubscription",
    },
    "node_architecture_graph_populate_effect": {
        "populate_from_contracts": "HandlerArchitectureGraphPopulate",
        "populate_from_imports": "HandlerArchitectureGraphPopulate",
        "populate_from_pyproject": "HandlerArchitectureGraphPopulate",
        "populate_all": "HandlerArchitectureGraphPopulate",
    },
    "node_architecture_graph_query_effect": {
        "dependency_path": "HandlerArchitectureGraphQuery",
        "blast_radius": "HandlerArchitectureGraphQuery",
        "cross_repo_imports": "HandlerArchitectureGraphQuery",
        "circular_deps": "HandlerArchitectureGraphQuery",
    },
    "node_intent_storage_effect": {
        "store": "HandlerIntentStorageAdapter",
        "get_session": "HandlerIntentStorageAdapter",
        "get_distribution": "HandlerIntentStorageAdapter",
    },
    "node_knowledge_query_federation_orchestrator": {
        "knowledge_query": "HandlerKnowledgeQueryFederation",
    },
    "node_memory_lifecycle_orchestrator": {
        "tick": "HandlerMemoryTick",
        "expire": "HandlerMemoryExpire",
        "archive": "HandlerMemoryArchive",
    },
    "node_memory_retrieval_effect": {
        "search": "HandlerMemoryRetrieval",
        "search_text": "HandlerMemoryRetrieval",
        "search_graph": "HandlerMemoryRetrieval",
    },
    "node_persona_lifecycle_orchestrator": {
        "on_tick": "HandlerPersonaRebuild",
        "on_demand": "HandlerPersonaRebuild",
    },
}
_LEGACY_ROUTE_FIELDS = {"routing_key", "handler_key", "priority", "output_events"}


def _contract(node_name: str) -> dict[str, Any]:
    path = _NODES_ROOT / node_name / "contract.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
@pytest.mark.parametrize(("node_name", "expected"), _MIGRATED_ROUTES.items())
def test_operation_routes_preserve_canonical_dispatch(
    node_name: str, expected: dict[str, str]
) -> None:
    contract = _contract(node_name)
    raw_routing = contract["handler_routing"]
    routing = ModelHandlerRoutingSubcontract.model_validate(raw_routing)

    assert routing.build_routing_table() == {
        operation: [handler_name] for operation, handler_name in expected.items()
    }
    assert {
        entry.operation: entry.handler.name for entry in routing.handlers
    } == expected
    assert all(
        not (_LEGACY_ROUTE_FIELDS & set(entry)) for entry in raw_routing["handlers"]
    )


@pytest.mark.unit
def test_agent_learning_route_preserves_default_handler_and_operation() -> None:
    contract = _contract("node_agent_learning_retrieval_effect")
    routing_data = contract["handler_routing"]
    routing = ModelHandlerRoutingSubcontract.model_validate(routing_data)

    assert routing.build_routing_table() == {
        "agent_learning_retrieval": ["HandlerAgentLearningRetrieval"]
    }
    assert routing_data["default_handler"].endswith(":HandlerAgentLearningRetrieval")


@pytest.mark.unit
def test_intent_query_uses_typed_event_route_and_handler_query_type() -> None:
    contract = _contract("node_intent_query_effect")
    routing_data = contract["handler_routing"]
    routing = ModelHandlerRoutingSubcontract.model_validate(routing_data)

    assert routing.build_routing_table() == {
        "ModelIntentQueryRequestedEvent": ["HandlerIntentQuery"]
    }
    assert routing.handlers[0].event_model is not None
    assert routing.handlers[0].operation is None
    assert all(
        not (_LEGACY_ROUTE_FIELDS & set(entry)) for entry in routing_data["handlers"]
    )


@pytest.mark.unit
def test_removed_output_events_remain_declared_as_publish_topics() -> None:
    """Event bus owns output-topic declarations after deprecated route metadata removal."""
    expected_topics = {
        "node_agent_coordinator_orchestrator": {
            "onex.evt.omnimemory.coordination-completed.v1",
            "onex.evt.omnimemory.memory-notification.v1",
        },
        "node_knowledge_query_federation_orchestrator": {
            "onex.evt.omnimarket.knowledge-query-federated.v1"
        },
        "node_persona_lifecycle_orchestrator": {
            "onex.evt.omnimemory.persona-snapshot-created.v1"
        },
    }
    for node_name, expected in expected_topics.items():
        contract = _contract(node_name)
        routing = contract["handler_routing"]
        publish_topics = set(contract["event_bus"]["publish_topics"])
        assert publish_topics == expected
        for entry in routing["handlers"]:
            assert not (_LEGACY_ROUTE_FIELDS & set(entry))


_REMOVED_ENTRY_FIELDS = _LEGACY_ROUTE_FIELDS | {"callable", "lazy_import"}


def _legacy_route_offenders(nodes_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(nodes_root.glob("node_*/contract.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        routing = loaded.get("handler_routing") if isinstance(loaded, dict) else None
        if not isinstance(routing, dict):
            continue
        for entry in routing.get("handlers") or []:
            if isinstance(entry, dict) and _REMOVED_ENTRY_FIELDS & set(entry):
                offenders.append(
                    f"{path.parent.name}: {sorted(_REMOVED_ENTRY_FIELDS & set(entry))}"
                )
    return offenders


@pytest.mark.unit
def test_no_node_contract_carries_legacy_route_fields() -> None:
    """No contract relies on the routing entry model ignoring removed fields."""
    assert _legacy_route_offenders(_NODES_ROOT) == []


@pytest.mark.unit
def test_legacy_route_scan_detects_removed_fields(tmp_path: Path) -> None:
    """Positive control: the scan reports an entry that still names a removed field."""
    node_dir = tmp_path / "node_fixture"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "handler_routing:\n"
        "  routing_strategy: payload_type_match\n"
        "  handlers:\n"
        "    - routing_key: a.b\n"
        "      handler_key: c\n"
        "      handler: {name: H, module: m}\n",
        encoding="utf-8",
    )
    assert _legacy_route_offenders(tmp_path) == [
        "node_fixture: ['handler_key', 'routing_key']"
    ]
