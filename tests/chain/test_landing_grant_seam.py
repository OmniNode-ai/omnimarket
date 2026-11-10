# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing chain's broker calls stay inside the dev principal's grants (OMN-23395).

The dev-lane principal's broker grants (the developer principal bindings of the lab
infrastructure declaration) grant exactly two topics -- the decision node's command and success
terminals -- and two consumer-group prefixes; the failure terminal is not granted. This
seam test spells the granted lists here and holds ``RedpandaDecisionBus`` against them,
so the chain the controller will run cannot drift onto a topic or a group the dev broker
refuses. It runs with no broker: the bus opener the bus imports inside ``start`` is
patched to yield a fake client that records every call. The control feeds the same
checks a group id under a prefix the grants do not name and demands red.
"""

from __future__ import annotations

import contextlib
import functools
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import pytest
from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from tests.chain.landing_chain import (
    DECISION_CONTRACT,
    ModelLandingChainOverlay,
    RedpandaDecisionBus,
    decision_topics,
)

pytestmark = pytest.mark.unit

# The dev-lane principal's grants (OMN-23395, the developer principal bindings of the
# lab infrastructure declaration), spelled here so the test needs no
# other repository.
GRANTED_COMMAND_TOPIC: Final = "onex.cmd.omnimarket.pr-landing-decision-requested.v1"
GRANTED_SUCCESS_TOPIC: Final = "onex.evt.omnimarket.pr-landing-decision-completed.v1"
GRANTED_TOPICS: Final[frozenset[str]] = frozenset(
    {GRANTED_COMMAND_TOPIC, GRANTED_SUCCESS_TOPIC}
)
NODE_GROUP_PREFIX: Final = "landing-chain-node-"
CONTROLLER_GROUP_PREFIX: Final = "landing-chain-controller-"
GRANTED_GROUP_PREFIXES: Final[tuple[str, str]] = (
    NODE_GROUP_PREFIX,
    CONTROLLER_GROUP_PREFIX,
)


def assert_grant_shape(
    subscriptions: Sequence[Mapping[str, Any]],
    publishes: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
) -> None:
    """Every broker-facing call recorded sits inside the dev principal's grants."""
    failure = decision_topics()["failure"]
    for sub in subscriptions:
        assert sub["topic"] in GRANTED_TOPICS, (
            f"subscribed {sub['topic']!r}, which the grants do not name"
        )
        assert sub["group_id"].startswith(GRANTED_GROUP_PREFIXES), (
            f"group id {sub['group_id']!r} is outside the granted prefixes"
        )
        if run_id is not None:
            assert sub["group_id"].endswith(run_id), (
                f"group id {sub['group_id']!r} does not end with run {run_id}"
            )
    for published in publishes:
        assert published["topic"] in GRANTED_TOPICS, (
            f"published {published['topic']!r}, which the grants do not name"
        )
    assert failure not in GRANTED_TOPICS, "the grants cover the failure terminal"
    carried = {sub["topic"] for sub in subscriptions} | {
        published["topic"] for published in publishes
    }
    assert failure not in carried, "the failure terminal left the bus"


class FakeLabRunBus:
    """A broker client that records the calls instead of reaching a broker."""

    def __init__(self) -> None:
        self.subscriptions: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.consumes_started = 0

    async def subscribe(self, topic: str, *, on_message: Any, group_id: str) -> None:
        self.subscriptions.append(
            {"topic": topic, "on_message": on_message, "group_id": group_id}
        )

    async def start_consuming(self) -> None:
        self.consumes_started += 1

    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        self.published.append({"topic": topic, "key": key, "value": value})


async def _started_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeLabRunBus, RedpandaDecisionBus]:
    """A redpanda bus started against the fake, the opener patched under it.

    The opener is patched where ``start`` imports it, and keeps the opener's
    ``*_home`` parameter, which ``start`` reads off the signature.
    """
    fake = FakeLabRunBus()

    # ``wraps`` carries the real opener's signature (``inspect.signature`` follows
    # ``__wrapped__``), so the registry-root parameter ``start`` looks up by its ``_home``
    # suffix is the real one.
    @contextlib.asynccontextmanager
    @functools.wraps(open_lab_run_bus)
    async def fake_open_lab_run_bus(**_: Any) -> AsyncIterator[FakeLabRunBus]:
        yield fake

    monkeypatch.setattr(
        "omnimarket.delegated_test_loop.lane_bus.open_lab_run_bus",
        fake_open_lab_run_bus,
    )
    bus = RedpandaDecisionBus(
        ModelLandingChainOverlay(profile="redpanda", lane="dev", consumer_join_s=0)
    )
    await bus.start()
    return fake, bus


async def test_decision_bus_subscribes_only_under_granted_topics_and_group_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bus = await _started_bus(monkeypatch)
    try:
        assert fake.consumes_started == 1
        assert_grant_shape(fake.subscriptions, fake.published, run_id=bus.run_id)
        # The two subscriptions are (command topic, node group) and
        # (success topic, controller group), each group named for this run.
        assert [(sub["topic"], sub["group_id"]) for sub in fake.subscriptions] == [
            (bus.topic_facts, f"{NODE_GROUP_PREFIX}{bus.run_id}"),
            (bus.topic_decision, f"{CONTROLLER_GROUP_PREFIX}{bus.run_id}"),
        ]
    finally:
        await bus.stop()


async def test_decision_bus_publishes_only_granted_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bus = await _started_bus(monkeypatch)
    try:
        assert fake.subscriptions
        await bus._send(bus.topic_facts, b"facts")
        await bus._send(bus.topic_decision, b"decision")
        assert {published["topic"] for published in fake.published} <= set(
            GRANTED_TOPICS
        )
        assert_grant_shape(fake.subscriptions, fake.published, run_id=bus.run_id)
        failure = decision_topics()["failure"]
        assert failure not in GRANTED_TOPICS
        assert failure not in {sub["topic"] for sub in fake.subscriptions}
        assert failure not in {published["topic"] for published in fake.published}
    finally:
        await bus.stop()


def test_topics_equal_the_node_contract() -> None:
    topics = decision_topics()
    assert topics["command"] == GRANTED_COMMAND_TOPIC
    assert topics["success"] == GRANTED_SUCCESS_TOPIC


def test_every_granted_topic_is_provisioned_by_a_node_contract() -> None:
    """A topic the principal is granted exists on the broker, or the chain times out.

    The provisioner creates what the contracts declare in ``consumed_events``,
    ``published_events`` and ``event_bus``, and not what ``runtime_dispatch`` names. The
    command topic is declared by the decision node and its success terminal by the
    ledger mirror node, which consumes it. A grant on a topic no contract declares
    is a grant on a topic that was never created.
    """
    decision_dir = DECISION_CONTRACT.parent
    mirror_dir = decision_dir.parent / "node_work_ledger_landing_mirror"
    provisioned = {
        entry.topic
        for node_dir in (decision_dir, mirror_dir)
        for entry in ContractTopicExtractor().extract(node_dir)
    }
    assert provisioned >= GRANTED_TOPICS, (
        f"granted but declared by no node contract: {sorted(GRANTED_TOPICS - provisioned)}"
    )


def test_control_a_changed_group_prefix_is_red() -> None:
    """A group id under a prefix the grants do not name fails the same checks."""
    off_grant: list[dict[str, Any]] = [
        {
            "topic": GRANTED_COMMAND_TOPIC,
            "on_message": None,
            "group_id": f"landing-chain-other-{uuid.uuid4().hex}",
        }
    ]
    with pytest.raises(AssertionError):
        assert_grant_shape(off_grant, [])
