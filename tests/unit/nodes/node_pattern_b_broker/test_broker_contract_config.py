"""Tests for Pattern B broker contract-driven runtime config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerOriginator,
    EnumPatternBBrokerRecipient,
)

_BROKER_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "contract.yaml"
)
_CONFIG_ADAPTER = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "handlers"
    / "adapter_broker_contract_config.py"
)


@pytest.mark.unit
def test_broker_config_loads_topics_from_contract() -> None:
    raw = yaml.safe_load(_BROKER_CONTRACT.read_text(encoding="utf-8"))
    event_bus = raw["event_bus"]

    config = load_pattern_b_broker_config(_BROKER_CONTRACT)

    assert config.topics.dispatch_request_topic == event_bus["subscribe_topics"][0]
    assert config.topics.terminal_completed_topic == event_bus["publish_topics"][0]
    assert config.topics.terminal_failed_topic == event_bus["publish_topics"][1]
    assert config.consumer_group == raw["broker"]["consumer_group"]
    assert (
        config.default_wait_policy.timeout_seconds
        == raw["broker"]["default_timeout_seconds"]
    )


@pytest.mark.unit
def test_broker_config_uses_enum_backed_allowlists() -> None:
    config = load_pattern_b_broker_config(_BROKER_CONTRACT)

    assert EnumPatternBBrokerOriginator.omnimarket in config.allowed_originators
    assert EnumPatternBBrokerRecipient.omniclaude in config.allowed_recipients


@pytest.mark.unit
def test_broker_config_rejects_string_wait_policy_fields(tmp_path: Path) -> None:
    raw = yaml.safe_load(_BROKER_CONTRACT.read_text(encoding="utf-8"))
    raw["broker"]["wait_for_terminal_event"] = "false"
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="wait_for_terminal_event"):
        load_pattern_b_broker_config(contract_path)


@pytest.mark.unit
def test_broker_config_rejects_bool_timeout_field(tmp_path: Path) -> None:
    raw = yaml.safe_load(_BROKER_CONTRACT.read_text(encoding="utf-8"))
    raw["broker"]["default_timeout_seconds"] = False
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="default_timeout_seconds"):
        load_pattern_b_broker_config(contract_path)


@pytest.mark.unit
def test_config_adapter_does_not_own_topic_literals() -> None:
    source = _CONFIG_ADAPTER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
