"""Contract config adapter for Pattern B broker handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    ModelPatternBBrokerRuntimeConfig,
    ModelPatternBBrokerTopicBindings,
    ModelPatternBBrokerWaitPolicy,
)

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def load_pattern_b_broker_config(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelPatternBBrokerRuntimeConfig:
    """Load broker runtime config from a node contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    broker = raw.get("broker")
    if not isinstance(broker, dict):
        raise ValueError(f"{contract_path} missing broker mapping")

    return ModelPatternBBrokerRuntimeConfig(
        topics=_load_topic_bindings(contract_path),
        consumer_group=_required_str(broker, "consumer_group", contract_path),
        default_wait_policy=ModelPatternBBrokerWaitPolicy(
            wait_for_terminal_event=bool(broker.get("wait_for_terminal_event", True)),
            timeout_seconds=int(broker.get("default_timeout_seconds", 300)),
        ),
        allowed_originators=tuple(_required_str_list(broker, "allowed_originators")),
        allowed_recipients=tuple(_required_str_list(broker, "allowed_recipients")),
    )


def _load_topic_bindings(contract_path: Path) -> ModelPatternBBrokerTopicBindings:
    subscribe_topics = contract_subscribe_topics(contract_path)
    publish_topics = contract_publish_topics(contract_path)
    return ModelPatternBBrokerTopicBindings(
        dispatch_request_topic=_single_topic(
            subscribe_topics,
            "delegate-task",
            contract_path,
        ),
        terminal_completed_topic=_single_topic(
            publish_topics,
            "delegation-completed",
            contract_path,
        ),
        terminal_failed_topic=_single_topic(
            publish_topics,
            "delegation-failed",
            contract_path,
        ),
    )


def _single_topic(topics: tuple[str, ...], fragment: str, contract_path: Path) -> str:
    matches = tuple(topic for topic in topics if fragment in topic)
    if len(matches) != 1:
        raise ValueError(
            f"{contract_path} expected exactly one topic containing {fragment!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _required_str(mapping: dict[Any, Any], key: str, contract_path: Path) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{contract_path} broker.{key} must be a non-empty string")
    return value


def _required_str_list(mapping: dict[Any, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"broker.{key} must be a string list")
    return value


__all__ = ["load_pattern_b_broker_config"]
