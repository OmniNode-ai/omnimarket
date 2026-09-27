# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_topic_activity_sampler_effect`` (OMN-19716, plan row E18).

onex.evt.platform.node-heartbeat.v1           (the clock; the lab has no live runtime tick)
    -> broker watermarks and offsets_for_times (read-only, group-less)
    -> onex.evt.omnimarket.topic-activity-sampled.v1 (one typed sample, split when large)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.events.topic_activity import ModelTopicActivitySampleEvent
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers._kafka_topic_activity_reader import (
    PartitionOffsetSnapshot,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers.handler_topic_activity_sampler import (
    HandlerTopicActivitySampler,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.models import (
    ModelTopicActivitySampleTrigger,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_topic_activity_sampler_effect"
    / "contract.yaml"
)
_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: the declared trigger topic
_SAMPLED_TOPIC = "onex.evt.omnimarket.topic-activity-sampled.v1"  # onex-topic-allow: this node's declared publish topic
_HOOK_TOPIC = "onex.evt.omniclaude.tool-executed.v1"  # onex-topic-allow: a lab topic used as broker fixture data
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _LabShapedReader:
    async def topic_partitions(self) -> dict[str, tuple[int, ...]]:
        return {"__consumer_offsets": (0,), _HOOK_TOPIC: (0,), "onex.evt.dead.v1": (0,)}

    async def watermarks_and_offsets_for_times(
        self,
        partitions: tuple[tuple[str, int], ...],
        *,
        one_hour_ms: int,
        one_day_ms: int,
    ) -> dict[tuple[str, int], PartitionOffsetSnapshot]:
        return {
            key: (
                PartitionOffsetSnapshot(0, 0, None, None)
                if key[0] == "onex.evt.dead.v1"
                else PartitionOffsetSnapshot(4_000, 70_000, 68_791, 5_083)
            )
            for key in partitions
        }

    async def timestamps_at_offsets(
        self, offsets: dict[tuple[str, int], int]
    ) -> dict[tuple[str, int], datetime]:
        return {key: _NOW - timedelta(seconds=1) for key in offsets}


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert contract["event_bus"]["subscribe_topics"] == [_HEARTBEAT_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [_SAMPLED_TOPIC]
    assert contract["terminal_event"] == _SAMPLED_TOPIC


def test_golden_chain_one_heartbeat_yields_one_sample_event() -> None:
    handler = HandlerTopicActivitySampler(
        reader=_LabShapedReader(), monotonic=lambda: 0.0, clock=lambda: _NOW
    )
    output = asyncio.run(handler.handle(ModelTopicActivitySampleTrigger()))
    assert len(output.events) == 1
    event = output.events[0]
    assert isinstance(event, ModelTopicActivitySampleEvent)
    assert event.total_topic_count == 2
    assert event.empty_topic_count == 1
    (sample,) = event.topics
    assert sample.topic == _HOOK_TOPIC
    assert sample.messages_last_hour == 70_000 - 68_791
    assert sample.messages_last_24h == 70_000 - 5_083
