from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONTRACT_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_llm_delegation_call_effect"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


def _topic_alias(topic: str) -> str:
    parts = topic.split(".")
    return f"{parts[2]}.{parts[3]}"


def test_multi_topic_handler_entries_pin_event_type_to_subscribe_topic() -> None:
    contract = _load_contract()
    subscribe_topics = tuple(contract["event_bus"]["subscribe_topics"])
    aliases_by_topic = {_topic_alias(topic): topic for topic in subscribe_topics}

    handlers = {
        handler["operation"]: handler
        for handler in contract["handler_routing"]["handlers"]
    }

    assert handlers["execute_delegation_call"]["event_type"] == (
        "omnimarket.delegation-execute"
    )
    assert handlers["execute_inference_intent"]["event_type"] == (
        "omnibase-infra.delegation-inference-request"
    )

    for handler in handlers.values():
        assert handler["event_type"] in aliases_by_topic


def test_payload_match_handlers_do_not_rely_on_ambiguous_multi_topic_binding() -> None:
    contract = _load_contract()
    subscribe_topics = tuple(contract["event_bus"]["subscribe_topics"])
    assert len(subscribe_topics) > 1

    missing = [
        handler["operation"]
        for handler in contract["handler_routing"]["handlers"]
        if handler.get("event_model") and not handler.get("event_type")
    ]

    assert missing == []
