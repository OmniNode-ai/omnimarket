# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The label-record command topic has a producer that the node's contract names."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest
import yaml

from omnimarket.events.delegation_eval import ModelLabelRecordRequest
from omnimarket.nodes.node_delegation_eval_orchestrator import publish_label_record

pytestmark = pytest.mark.unit

_TENANT = "11111111-1111-1111-1111-111111111111"


_CONTRACT = Path(publish_label_record.__file__).parent / "contract.yaml"


def _load() -> ModuleType:
    return publish_label_record


def _line(**over: object) -> str:
    row: dict[str, object] = {
        "correlation_id": "call-1",
        "attempt_index": 0,
        "label": "adequate",
        "rater_role": "human",
        "rubric_version": "v1",
        "stratum": "summarization/accepted",
        "computed_facts": {"manifest_id": "m1"},
    }
    row.update(over)
    return json.dumps(row)


class _FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def send(self, topic: str, value: dict[str, object]) -> None:
        self.sent.append((topic, value))


def test_topic_is_the_contract_command_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert _load().command_topic() == contract["runtime_dispatch"]["command_topic"]
    assert _load().command_topic() in contract["event_bus"]["subscribe_topics"]


def test_each_label_is_published_as_a_valid_bare_request() -> None:
    mod = _load()
    producer = _FakeProducer()
    count = mod.publish_labels(
        [_line(), _line(correlation_id="call-2", label="inadequate")],
        tenant_id=_TENANT,
        producer=producer,
    )
    assert count == 2
    assert {topic for topic, _ in producer.sent} == {mod.command_topic()}
    for _, value in producer.sent:
        request = ModelLabelRecordRequest.model_validate(value)
        assert request.tenant_id == UUID(_TENANT)
    assert [v["correlation_id"] for _, v in producer.sent] == ["call-1", "call-2"]


def test_invalid_label_publishes_nothing() -> None:
    mod = _load()
    producer = _FakeProducer()
    with pytest.raises(SystemExit):
        mod.publish_labels(
            [_line(), _line(label="")], tenant_id=_TENANT, producer=producer
        )
    assert producer.sent == []


def test_bad_tenant_is_refused() -> None:
    with pytest.raises(SystemExit):
        _load().publish_labels(
            [_line()], tenant_id="not-a-uuid", producer=_FakeProducer()
        )
