# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The house-routing-overlay-write command topic has a producer and a runtime profile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_house_routing_overlay_effect import (
    publish_house_overlay_command,
)

pytestmark = pytest.mark.unit

_CONTRACT = Path(publish_house_overlay_command.__file__).parent / "contract.yaml"

_DECLARATION: dict[str, object] = {
    "backend_id": "lab-qwen",
    "endpoint_url": "http://192.0.2.1:8000/v1/chat/completions",
    "model_name": "qwen",
    "provider": "lab",
    "tier_name": "local",
    "task_types": ["summarization"],
}
_DECLARE: dict[str, object] = {"operation": "declare", "declaration": _DECLARATION}
_RETIRE: dict[str, object] = {
    "operation": "retire",
    "retire_backend_id": "lab-qwen",
    "retire_task_types": ["summarization"],
}


class _FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def send(self, topic: str, value: dict[str, object]) -> None:
        self.sent.append((topic, value))


def test_topic_is_the_contract_command_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    topic = publish_house_overlay_command.command_topic()
    assert topic == contract["runtime_dispatch"]["command_topic"]
    assert topic in contract["event_bus"]["subscribe_topics"]


@pytest.mark.parametrize("body", [_DECLARE, _RETIRE])
def test_one_valid_command_is_published_bare(body: dict[str, object]) -> None:
    producer = _FakeProducer()
    count = publish_house_overlay_command.publish_command(json.dumps(body), producer)
    assert count == 1
    topic, value = producer.sent[0]
    assert topic == publish_house_overlay_command.command_topic()
    assert value["operation"] == body["operation"]
    assert "tenant_id" not in value


@pytest.mark.parametrize(
    "body",
    [
        {**_DECLARE, "declaration": {**_DECLARATION, "task_types": ["*"]}},
        {"operation": "retire", "retire_backend_id": "x"},
        {**_DECLARE, "tenant_id": "customer"},
        "not json",
    ],
)
def test_invalid_command_publishes_nothing(body: object) -> None:
    producer = _FakeProducer()
    text = body if isinstance(body, str) else json.dumps(body)
    with pytest.raises(SystemExit):
        publish_house_overlay_command.publish_command(text, producer)
    assert producer.sent == []


def test_contract_declares_a_runtime_profile_the_runtime_reads() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert contract["runtime_profiles"] == ["effects"]
