# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step and the tick orchestrator stay inside the dev principal's grants (OMN-23494).

The dev-lane principal's broker grants for the landing snapshot record (the developer principal
bindings of the lab infrastructure declaration, OMN-23468) let the principal produce and consume the
tick-facts record topic, produce the snapshot failure topic, and join consumer groups under two
prefixes: ``landing-snapshot-join-`` (the join test) and ``landing-tick-orchestrator-`` (the tick
orchestrator). This seam test spells those lists here and holds both real adapters against them,
the snapshot step's lane publisher and the tick orchestrator's bus reader, so either side drifting
onto a topic or group the dev broker refuses turns it red. It runs with no broker: the lane bus
opener both adapters import inside their methods is patched to yield a fake that records each call.
The control feeds a group under a prefix the grants do not name and demands red.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_topic_reader import (
    BusTickTopicReader,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickTopicSelection,
)
from omnimarket.topic_namespace import apply_topic_namespace
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    scenario_facts,
)

pytestmark = pytest.mark.unit

# The dev-lane principal's grants (OMN-23468, the developer principal bindings of the lab
# infrastructure declaration), spelled here so the test needs no other repository.
RECORD_TOPIC: Final = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"
FAILED_TOPIC: Final = "onex.evt.omnimarket.pr-landing-snapshot-failed.v1"
GRANTED_PRODUCE: Final[frozenset[str]] = frozenset({RECORD_TOPIC, FAILED_TOPIC})
GRANTED_CONSUME: Final[frozenset[str]] = frozenset({RECORD_TOPIC})
JOIN_GROUP_PREFIX: Final = "landing-snapshot-join-"
TICK_GROUP_PREFIX: Final = "landing-tick-orchestrator-"
GRANTED_GROUP_PREFIXES: Final[tuple[str, str]] = (JOIN_GROUP_PREFIX, TICK_GROUP_PREFIX)

SNAPSHOT_OVERLAY = (
    Path(__file__).parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_snapshot_effect"
    / "snapshot.overlay.redpanda.example.yaml"
)


def assert_grant_shape(
    subscriptions: Sequence[tuple[str, str]], publishes: Sequence[str]
) -> None:
    """Every broker-facing call recorded sits inside the dev principal's grants."""
    granted_consume = {apply_topic_namespace(t) for t in GRANTED_CONSUME}
    granted_produce = {apply_topic_namespace(t) for t in GRANTED_PRODUCE}
    for topic, group in subscriptions:
        assert topic in granted_consume, (
            f"subscribed {topic!r}, which the grants do not name"
        )
        assert group.startswith(GRANTED_GROUP_PREFIXES), (
            f"group id {group!r} is outside the granted prefixes"
        )
    for topic in publishes:
        assert topic in granted_produce, (
            f"published {topic!r}, which the grants do not name"
        )


class _Handle:
    async def stop(self) -> None:
        return None


class FakeLaneBus:
    """A lane bus that records the calls instead of reaching a broker."""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.published: list[str] = []
        self.subscriptions: list[tuple[str, str]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        del key, value
        self.published.append(topic)

    async def subscribe(
        self, topic: str, *, on_message: Any, group_id: str, auto_offset_reset: str
    ) -> _Handle:
        del on_message, auto_offset_reset
        self.subscriptions.append((topic, group_id))
        return _Handle()


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> FakeLaneBus:
    fake = FakeLaneBus()

    @contextlib.asynccontextmanager
    async def fake_open_lane_bus(**kwargs: Any) -> AsyncIterator[FakeLaneBus]:
        fake.opened.append(kwargs)
        yield fake

    # both adapters import the opener inside their methods, so patch it where it lives
    monkeypatch.setattr(
        "omnimarket.handlers.overlay_store.bus_opener.open_lane_bus",
        fake_open_lane_bus,
    )
    return fake


def _snapshot_overlay() -> dict[str, Any]:
    return yaml.safe_load(SNAPSHOT_OVERLAY.read_text(encoding="utf-8"))


def _record() -> ModelLandingTickRecord:
    return ModelLandingTickRecord(
        correlation_id=FIXED_ID,
        tick=FIRST,
        facts=scenario_facts(FIRST),
        provenance="grant-seam",
    )


def _selection(group: str) -> ModelTickTopicSelection:
    return ModelTickTopicSelection(
        topic=_snapshot_overlay()["record_topic"],
        consumer_group=group,
        bus_lane="dev",
        workspace_root="/ws",
        join_timeout_s=1.0,
    )


def test_the_snapshot_publisher_publishes_only_granted_topics(
    fake_bus: FakeLaneBus,
) -> None:
    step = _snapshot_overlay()
    publisher = LaneBusSnapshotPublisher(step["bus_lane"], Path("/ws"))
    publisher.publish(step["record_topic"], _record())
    publisher.publish(step["failed_topic"], _record())

    assert fake_bus.published == [
        apply_topic_namespace(RECORD_TOPIC),
        apply_topic_namespace(FAILED_TOPIC),
    ]
    assert [opened["lane"] for opened in fake_bus.opened] == ["dev", "dev"]
    assert_grant_shape(fake_bus.subscriptions, fake_bus.published)


def test_the_tick_reader_subscribes_only_granted_topics_under_granted_groups(
    fake_bus: FakeLaneBus,
) -> None:
    run = uuid.uuid4().hex
    for group in (f"{TICK_GROUP_PREFIX}{run}", f"{JOIN_GROUP_PREFIX}{run}"):
        assert BusTickTopicReader(_selection(group)).read(1, 0.01) == []

    assert fake_bus.subscriptions == [
        (apply_topic_namespace(RECORD_TOPIC), f"{TICK_GROUP_PREFIX}{run}"),
        (apply_topic_namespace(RECORD_TOPIC), f"{JOIN_GROUP_PREFIX}{run}"),
    ]
    assert [opened["lane"] for opened in fake_bus.opened] == ["dev", "dev"]
    assert_grant_shape(fake_bus.subscriptions, fake_bus.published)


def test_control_a_group_outside_the_granted_prefixes_is_red(
    fake_bus: FakeLaneBus,
) -> None:
    run = uuid.uuid4().hex
    BusTickTopicReader(_selection(f"landing-snapshot-{run}")).read(1, 0.01)

    with pytest.raises(AssertionError, match="outside the granted prefixes"):
        assert_grant_shape(fake_bus.subscriptions, fake_bus.published)


def test_control_a_topic_the_grants_do_not_name_is_red(fake_bus: FakeLaneBus) -> None:
    LaneBusSnapshotPublisher("dev", Path("/ws")).publish(
        "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v2", _record()
    )

    with pytest.raises(AssertionError, match="which the grants do not name"):
        assert_grant_shape(fake_bus.subscriptions, fake_bus.published)
