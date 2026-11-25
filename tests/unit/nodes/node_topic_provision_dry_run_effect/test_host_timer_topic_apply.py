# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The apply creates the publish topics a host-timer contract declares, and only those."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.models.topic_provision.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_writer_topic_apply import (
    HandlerWriterTopicApply,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)

pytestmark = pytest.mark.unit

TIMER_TOPIC = "onex.evt.omnimarket.example-timer-daily-record.v1"
ROUTED_TOPIC = "onex.evt.omnimarket.example-routed-result.v1"

TIMER_CONTRACT = f"""\
name: node_example_timer
contract_name: node_example_timer
runtime_profiles: [effects]
runtime_dispatch:
  external_trigger: true
  host_unit:
    unit: example-timer-systemd
event_bus:
  subscribe_topics: []
  publish_topics:
    - {TIMER_TOPIC}
"""

ROUTED_CONTRACT = f"""\
name: node_example_routed
contract_name: node_example_routed
runtime_profiles: [effects]
event_bus:
  subscribe_topics: []
  publish_topics:
    - {ROUTED_TOPIC}
"""


class FakeBroker:
    def __init__(self) -> None:
        self.live: list[str] = []
        self.creates: list[tuple[str, int, int]] = []

    async def list_topics(self) -> tuple[str, ...]:
        return tuple(sorted(self.live))

    async def create_topic(self, name: str, partitions: int, replicas: int) -> bool:
        self.creates.append((name, partitions, replicas))
        if name in self.live:
            return False
        self.live.append(name)
        return True


@pytest.fixture
def request_with_timer(tmp_path: Path) -> ModelTopicProvisionDryRunRequest:
    for name, text in (
        ("node_example_timer", TIMER_CONTRACT),
        ("node_example_routed", ROUTED_CONTRACT),
    ):
        node = tmp_path / "contracts" / name
        node.mkdir(parents=True)
        (node / "contract.yaml").write_text(text, encoding="utf-8")
    return ModelTopicProvisionDryRunRequest(
        overlay=ModelTopicProvisionOverlay(
            profile="effects", contract_roots=(str(tmp_path / "contracts"),)
        ),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.ALL),
    )


async def test_apply_creates_the_host_timer_publish_topic_once(
    request_with_timer: ModelTopicProvisionDryRunRequest,
) -> None:
    broker = FakeBroker()
    handler = HandlerWriterTopicApply(broker, broker)

    first = await handler.handle(request_with_timer)
    broker.creates.clear()
    second = await handler.handle(request_with_timer)

    assert first.created == (TIMER_TOPIC,)
    assert second.created == ()
    assert broker.creates == []


async def test_apply_leaves_a_routed_contracts_topic_to_the_runtime_boot(
    request_with_timer: ModelTopicProvisionDryRunRequest,
) -> None:
    broker = FakeBroker()

    await HandlerWriterTopicApply(broker, broker).handle(request_with_timer)

    assert ROUTED_TOPIC not in broker.live
