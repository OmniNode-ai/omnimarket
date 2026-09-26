# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_topic_activity`` (OMN-19716, plan row E18).

    onex.evt.omnimarket.topic-activity-sampled.v1   (one sample per cycle, from the sampler effect)
        -> omninode_internal.topic_activity           (one row per topic)
        -> onex.snapshot.projection.topic-activity.v1 (bus-backed exposure)
        -> onex.evt.omnimarket.projection-topic-activity-applied.v1 (terminal)

Numbers are the lab's own for the hook topic on 2026-09-26 (census: 64,917
messages in 24 h, about 1,209 in the hour before the read).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.topic_activity import ModelTopicActivitySample
from omnimarket.nodes.node_projection_topic_activity.handlers.handler_projection_topic_activity import (
    HandlerProjectionTopicActivity,
)
from omnimarket.nodes.node_projection_topic_activity.handlers.handler_topic_activity_writer import (
    TopicActivityProjectionWriter,
)
from omnimarket.nodes.node_projection_topic_activity.models import (
    EnumTopicActivityState,
    ModelTopicActivityProjectionRequest,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_topic_activity"
    / "contract.yaml"
)
_SAMPLE_TOPIC = "onex.evt.omnimarket.topic-activity-sampled.v1"  # onex-topic-allow: the sampler effect's declared publish topic
_SNAPSHOT_TOPIC = "onex.snapshot.projection.topic-activity.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-topic-activity-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-topic-activity-malformed.v1"  # onex-topic-allow: this node's declared DLQ
_HOOK_TOPIC = "onex.evt.omniclaude.tool-executed.v1"  # onex-topic-allow: a lab topic used as fold input data
_T0 = datetime(2026, 9, 26, 11, 0, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract()
    event_bus = contract["event_bus"]
    assert event_bus["subscribe_topics"] == [_SAMPLE_TOPIC]
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["projection_api"]["bus_backed"] is True
    golden_path = contract["golden_path"]
    assert any("ABSENT" in step for step in golden_path)
    assert all("delete" not in step.lower() for step in golden_path)


def test_golden_chain_two_samples_give_an_unknown_rate_then_a_real_one() -> None:
    fold = HandlerProjectionTopicActivity()
    first = fold.handle(
        ModelTopicActivityProjectionRequest(
            topic=_HOOK_TOPIC,
            sampled_at=_T0,
            sample=ModelTopicActivitySample(
                topic=_HOOK_TOPIC,
                high_watermark_total=70_000,
                low_watermark_total=4_000,
                messages_last_hour=1_209,
                messages_last_24h=64_917,
                retention_truncated=False,
                newest_message_at=_T0 - timedelta(seconds=1),
            ),
        )
    )
    assert first.row.rate_per_second is None
    assert first.row.activity_state == EnumTopicActivityState.ACTIVE

    later = _T0 + timedelta(seconds=30)
    second = fold.handle(
        ModelTopicActivityProjectionRequest(
            topic=_HOOK_TOPIC,
            sampled_at=later,
            previous_row=first.row,
            sample=ModelTopicActivitySample(
                topic=_HOOK_TOPIC,
                high_watermark_total=70_012,
                low_watermark_total=4_000,
                messages_last_hour=1_215,
                messages_last_24h=64_929,
                retention_truncated=False,
                newest_message_at=later - timedelta(seconds=2),
            ),
        )
    )
    assert second.row.messages_since_previous_sample == 12
    assert second.row.rate_per_second == pytest.approx(0.4)
    assert second.row.messages_last_hour == 1_215


def test_golden_chain_terminal_is_the_applied_topic_and_only_the_writer_is_routed() -> (
    None
):
    contract = _contract()
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    routed = [
        entry["handler"]["name"] for entry in contract["handler_routing"]["handlers"]
    ]
    assert routed == ["TopicActivityProjectionWriter"]
    assert TopicActivityProjectionWriter.onex_runtime_inprocess_dispatch is True
