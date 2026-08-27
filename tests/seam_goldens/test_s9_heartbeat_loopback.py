# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S9 — the gateway-heartbeat topic declared on both mirror sides.

``onex.evt.omnibase-infra.gateway-heartbeat.v1`` appears in the packaged
contract's ``mirror_topics.inbound`` AND its ``mirror_topics.outbound``. Every
inbound entry becomes a cloud subscribe topic and every outbound entry becomes
a cloud publish target, so the process subscribes to the heartbeat it itself
publishes. The registry classifies this MISMATCH at medium severity.

**Scope honesty.** This edge is goldened but NOT claimed as traversed by the
slice. Heartbeat continuity belongs to the receipt's credential-lifecycle
binding item, which is a different binding item from the business-path one this
ticket is scoped to; the manifest records it as ``flagged_ambiguous`` and the
binding guard asserts such rows never claim traversal. It is included anyway
because it is a live defect either way, and because it is cheaply and
completely drivable through real code.

The golden drives the actual loopback: ``publish_heartbeat()`` emits onto the
cloud leg, and those exact captured bytes are fed straight back in as an
inbound message on the same wire topic. The forwarder's ``gateway_direction``
guard is what stops the loop — so the defect today is contained by a runtime
tag rather than by the topic sets being correct, and that containment is
itself worth pinning: if the guard is ever removed while the contract still
declares both sides, the gateway starts consuming its own heartbeats.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_forwarder import (
    ServiceGatewayForwarder,
)

from tests.seam_goldens.harness import (
    GATEWAY_TENANT_SLUG,
    BusMessage,
    RecordingPublisher,
    assert_registry_classification,
    build_forwarder_config,
    consumer_projection,
    gateway_mirror_topics,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_HEARTBEAT_TOPIC = "onex.evt.omnibase-infra.gateway-heartbeat.v1"
_HEARTBEAT_WIRE = f"tenant-{GATEWAY_TENANT_SLUG}.{_HEARTBEAT_TOPIC}"


class TestTheTopicIsDeclaredOnBothSides:
    def test_slice_row_is_flagged_ambiguous_and_not_traversed(self) -> None:
        """The scope claim is recorded honestly, not upgraded."""

        edge = slice_edge("S9")
        assert edge.inclusion.value == "flagged_ambiguous"
        assert edge.traversed is False
        assert edge.registry_classification == "MISMATCH"

    def test_heartbeat_is_in_both_mirror_topic_sets(self) -> None:
        mirror = gateway_mirror_topics()
        assert _HEARTBEAT_TOPIC in mirror.inbound
        assert _HEARTBEAT_TOPIC in mirror.outbound

    def test_it_is_the_only_topic_declared_on_both_sides(self) -> None:
        """Bound the defect: one overlapping topic, not a systemic pattern.

        Naming the size of the problem matters — if a future contract edit adds
        more overlaps, this fails and the edge needs re-scoping rather than
        silently growing under an unchanged registry row.
        """

        mirror = gateway_mirror_topics()
        overlap = set(mirror.inbound) & set(mirror.outbound)
        assert overlap == {_HEARTBEAT_TOPIC}


class TestTheLoopbackIsRealAndContained:
    """Drive publish -> re-consume with the real service and real bytes."""

    async def test_heartbeat_publishes_to_the_tenant_wire_topic(
        self, tmp_path: Path
    ) -> None:
        cloud_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=RecordingPublisher(),
            cloud_bus=cloud_bus,
        )

        await forwarder.publish_heartbeat()

        published = cloud_bus.only()
        assert published.topic == _HEARTBEAT_WIRE

    async def test_published_heartbeat_is_tagged_local_to_cloud(
        self, tmp_path: Path
    ) -> None:
        cloud_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=RecordingPublisher(),
            cloud_bus=cloud_bus,
        )

        await forwarder.publish_heartbeat()

        tags = cloud_bus.only().envelope().metadata.tags
        assert tags["gateway_direction"] == "local-to-cloud"

    async def test_the_process_would_re_consume_its_own_heartbeat_but_for_the_guard(
        self, tmp_path: Path
    ) -> None:
        """The defect, driven: same bytes, same topic, back in the front door.

        The contract genuinely routes the gateway's own heartbeat back to it —
        the ONLY thing preventing an infinite republish loop is the
        ``gateway_direction`` tag check inside ``_consume_inbound_message``,
        not the topic sets being disjoint. This asserts the loop is suppressed
        while the topic overlap that causes it still exists, which is the
        precise shape of the MISMATCH.

        Why this no longer asserts ``local_bus.published == []`` (OMN-16794).
        The 0.38.9 -> 0.38.10 bump made ``publish_heartbeat`` mirror the SAME
        envelope onto the local canonical topic as well as the cloud wire topic
        (OMN-15570 G3): ``NodeGatewayLinkHealthProjectionCompute`` subscribes on
        the LOCAL bus, so before that fix heartbeats only ever reached the cloud
        leg and the projection never saw a live event. So one local publish is
        now correct and deliberate, and an empty-list assertion would fail on
        the FIX rather than on the defect.

        The guard was re-verified directly rather than assumed: local publishes
        are 1 after ``publish_heartbeat`` and still 1 after
        ``consume_inbound_message``. The assertion below is therefore the
        sharper form of the original question — does re-consuming its own
        heartbeat make the gateway publish AGAIN — and it stays red if the
        ``gateway_direction`` check is ever removed.
        """

        local_bus = RecordingPublisher()
        cloud_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=local_bus,
            cloud_bus=cloud_bus,
        )

        await forwarder.publish_heartbeat()
        emitted = cloud_bus.only()

        # The deliberate OMN-15570 G3 local mirror, pinned so that a future
        # change to the dual-publish shape surfaces here instead of silently
        # widening the baseline this test subtracts.
        published_by_the_heartbeat_itself = list(local_bus.published)
        assert len(published_by_the_heartbeat_itself) == 1, (
            "expected exactly one deliberate local mirror from publish_heartbeat "
            f"(OMN-15570 G3), got {len(published_by_the_heartbeat_itself)}"
        )

        # The heartbeat the gateway just published arrives on the very topic
        # mirror_topics.inbound tells it to subscribe to.
        assert emitted.topic == _HEARTBEAT_WIRE
        await forwarder.consume_inbound_message(
            BusMessage(topic=emitted.topic, value=emitted.value)
        )

        assert local_bus.published == published_by_the_heartbeat_itself, (
            "gateway re-consumed and republished its own heartbeat; the "
            "loopback guard is gone while the topic overlap remains"
        )


class TestS9RegistryMatch:
    def test_registry_match_reports_mismatch_on_the_direction(self) -> None:
        verdict = run_registry_match(
            edge_id="S9",
            declared_producer=producer_projection(
                edge_id="S9",
                topic=_HEARTBEAT_TOPIC,
                key_fields=(("gateway_direction", "local-to-cloud"),),
            ),
            declared_consumer=consumer_projection(
                edge_id="S9",
                topic=_HEARTBEAT_TOPIC,
                key_fields=(("gateway_direction", "cloud-to-local"),),
            ),
        )

        assert_registry_classification("S9", verdict)
        assert verdict.leg1_declared_vs_declared.passed is False
        assert verdict.regenerability.value == "NOT_APPLICABLE"
