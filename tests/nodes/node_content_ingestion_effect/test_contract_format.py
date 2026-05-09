# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-7873: verify node_content_ingestion_effect contract uses omnimarket format.

Guards against regression to the omniintelligence-style format that used
routing: arrays and filter: expressions in the contract YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_content_ingestion_effect"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    with CONTRACT_PATH.open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict)
    return contract


def test_contract_file_exists() -> None:
    assert CONTRACT_PATH.exists(), f"contract.yaml not found at {CONTRACT_PATH}"


def test_contract_required_fields_present() -> None:
    contract = _load_contract()
    assert "name" in contract
    assert "node_type" in contract
    assert "handler" in contract
    assert contract["name"] == "node_content_ingestion_effect"
    assert contract["node_type"].upper() == "EFFECT"


def test_contract_handler_block_is_omnimarket_format() -> None:
    contract = _load_contract()
    handler = contract["handler"]
    assert isinstance(handler, dict), "handler must be a dict, not a list"
    assert "module" in handler
    assert "class" in handler
    assert handler["module"].startswith(
        "omnimarket.nodes.node_content_ingestion_effect"
    )


def test_contract_no_omniintelligence_routing_keys() -> None:
    """Regression guard: omniintelligence-style keys must not appear."""
    contract = _load_contract()
    handler = contract["handler"]

    # The wrong format used handler.routing as a list of {handler, subscribe, filter}
    assert "routing" not in handler, (
        "handler.routing (list-style) is omniintelligence format — "
        "content-type dispatch belongs in handler_routing.py code, not contract YAML"
    )

    # filter: expressions are not a supported contract feature in omnimarket
    def _has_filter_key(obj: Any) -> bool:
        if isinstance(obj, dict):
            if "filter" in obj:
                return True
            return any(_has_filter_key(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_has_filter_key(item) for item in obj)
        return False

    assert not _has_filter_key(contract), (
        "filter: expression keys found in contract — "
        "content-type routing belongs in handler code, not YAML predicates"
    )


def test_contract_event_bus_has_correct_topics() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    subscribe = event_bus["subscribe_topics"]
    publish = event_bus["publish_topics"]

    assert "onex.cmd.omnimarket.content-ingestion-start.v1" in subscribe
    assert "onex.evt.omnimarket.content-discovered.v1" in subscribe
    assert "onex.evt.omnimarket.content-extracted.v1" in publish
    assert "onex.evt.omnimarket.content-ingestion-completed.v1" in publish


def test_contract_terminal_event_declared() -> None:
    contract = _load_contract()
    assert "terminal_event" in contract
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.content-ingestion-completed.v1"
    )


def test_contract_topics_use_omnimarket_namespace() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    all_topics = event_bus.get("subscribe_topics", []) + event_bus.get(
        "publish_topics", []
    )
    for topic in all_topics:
        assert "omnimarket" in topic, f"topic {topic!r} must use omnimarket namespace"


@pytest.mark.parametrize(
    "forbidden_key",
    ["routing", "filter", "subscribe", "publish", "env_dependencies"],
)
def test_contract_handler_block_has_no_omniintelligence_keys(
    forbidden_key: str,
) -> None:
    contract = _load_contract()
    handler = contract["handler"]
    assert forbidden_key not in handler, (
        f"handler.{forbidden_key} is an omniintelligence contract key — "
        f"omnimarket contracts use handler.module + handler.class"
    )
