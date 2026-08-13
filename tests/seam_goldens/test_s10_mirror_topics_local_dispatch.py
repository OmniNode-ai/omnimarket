# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S10 — mirror_topics republication vs omnimarket's declared local dispatch.

This is the processing hop's fan-in/fan-out, and the receipt traverses it in
both directions: the inbound command arrives on a bare topic after the gateway
strips the tenant prefix, and the completion leaves on a bare topic the same
local contract publishes.

The seam is the *string identity* between two independently maintained
declarations — the gateway contract's ``mirror_topics`` (in the pinned
``omnibase_infra`` wheel) and ``node_delegation_orchestrator``'s
``subscribe_topics`` / ``publish_topics`` (in this repo). Neither side imports
the other, so a rename on either side is invisible to both test suites. Both
sets are read from their real files here and compared directly, then the
republish is actually driven through ``ServiceGatewayForwarder`` so the golden
proves the topic a node would receive, not merely the topic a YAML file names.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_forwarder import (
    ServiceGatewayForwarder,
)

from tests.seam_goldens.harness import (
    GATEWAY_PRINCIPAL_ID,
    GATEWAY_TENANT_ID,
    GATEWAY_TENANT_SLUG,
    BusMessage,
    RecordingPublisher,
    assert_correlation_preserved,
    assert_registry_classification,
    build_forwarder_config,
    cloud_hand_rolled_envelope_json,
    consumer_projection,
    gateway_mirror_topics,
    local_typed_envelope,
    omnimarket_node_event_bus,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_LOCAL_RUNTIME_NODE = "node_delegation_orchestrator"
_INBOUND_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_OUTBOUND_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"

_S10_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("correlation_id", "UUID"),
    ("payload", "dict[str, object]"),
)


class TestMirrorTopicsAreBareCanonicalStrings:
    """The republished form must be bare — a prefix here breaks local dispatch."""

    def test_slice_row_covers_both_directions(self) -> None:
        edge = slice_edge("S10")
        assert edge.traversed
        assert "both directions" in edge.leg

    def test_no_mirror_topic_carries_a_tenant_prefix(self) -> None:
        mirror = gateway_mirror_topics()
        for topic in (*mirror.inbound, *mirror.outbound):
            assert not topic.startswith("tenant-"), topic


class TestInboundDirectionReachesTheLocalSubscriber:
    """gateway mirror_topics.inbound -> omnimarket subscribe_topics."""

    def test_declared_inbound_command_is_subscribed_locally(self) -> None:
        assert _INBOUND_TOPIC in gateway_mirror_topics().inbound
        assert (
            _INBOUND_TOPIC
            in omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE).subscribe_topics
        )

    async def test_republished_topic_is_the_topic_the_node_subscribes_to(
        self, tmp_path: Path
    ) -> None:
        """Drive the real republish and compare against the real contract.

        The assertion compares the topic the forwarder ACTUALLY published to
        against the local contract's declared subscription set. A string-to-
        string comparison of two YAML files could pass while the runtime
        republished something else; this cannot.
        """

        local_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=local_bus,
            cloud_bus=RecordingPublisher(),
        )
        correlation_id = uuid4()

        await forwarder.consume_inbound_message(
            BusMessage(
                topic=f"tenant-{GATEWAY_TENANT_SLUG}.{_INBOUND_TOPIC}",
                value=cloud_hand_rolled_envelope_json(
                    envelope_id=uuid4(),
                    correlation_id=correlation_id,
                    event_type="omnibase-infra.delegation-request",
                    payload={"prompt": "p", "task_type": "summarization"},
                    source_tenant_id=str(GATEWAY_TENANT_ID),
                    source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
                ),
            )
        )

        published = local_bus.only()
        assert (
            published.topic
            in omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE).subscribe_topics
        )
        assert_correlation_preserved(
            edge_id="S10",
            emitted=correlation_id,
            observed=published.envelope().correlation_id,
        )


class TestOutboundDirectionLeavesTheLocalPublisher:
    """omnimarket publish_topics -> gateway mirror_topics.outbound."""

    def test_declared_outbound_completion_is_published_locally(self) -> None:
        assert _OUTBOUND_TOPIC in gateway_mirror_topics().outbound
        assert (
            _OUTBOUND_TOPIC
            in omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE).publish_topics
        )

    async def test_locally_published_topic_is_mirrored_outbound(
        self, tmp_path: Path
    ) -> None:
        cloud_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=RecordingPublisher(),
            cloud_bus=cloud_bus,
        )
        correlation_id = uuid4()

        await forwarder.forward_outbound_message(
            BusMessage(
                topic=_OUTBOUND_TOPIC,
                value=local_typed_envelope(
                    envelope_id=uuid4(),
                    correlation_id=correlation_id,
                    event_type="omnibase-infra.delegation-completed",
                    payload={"status": "completed", "tenant_id": GATEWAY_TENANT_SLUG},
                )
                .model_dump_json(exclude_none=True)
                .encode("utf-8"),
            )
        )

        published = cloud_bus.only()
        assert published.topic == f"tenant-{GATEWAY_TENANT_SLUG}.{_OUTBOUND_TOPIC}"
        assert_correlation_preserved(
            edge_id="S10",
            emitted=correlation_id,
            observed=published.envelope().correlation_id,
        )

    async def test_a_topic_absent_from_mirror_outbound_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Negative control: mirroring is allowlist-driven, not pass-through."""

        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=RecordingPublisher(),
            cloud_bus=RecordingPublisher(),
        )
        undeclared = "onex.evt.omnibase-infra.not-mirrored-anywhere.v1"
        assert undeclared not in gateway_mirror_topics().outbound

        with pytest.raises(ValueError, match="not declared for outbound mirroring"):
            await forwarder.forward_outbound_message(
                BusMessage(
                    topic=undeclared,
                    value=local_typed_envelope(
                        envelope_id=uuid4(),
                        correlation_id=uuid4(),
                        event_type="omnibase-infra.whatever",
                        payload={},
                    )
                    .model_dump_json(exclude_none=True)
                    .encode("utf-8"),
                )
            )


class TestS10RegistryMatch:
    def test_registry_match_is_regenerable_for_the_driven_seam(self) -> None:
        declared_producer = producer_projection(
            edge_id="S10", topic=_INBOUND_TOPIC, key_fields=_S10_KEY_FIELDS
        )
        declared_consumer = consumer_projection(
            edge_id="S10", topic=_INBOUND_TOPIC, key_fields=_S10_KEY_FIELDS
        )

        verdict = run_registry_match(
            edge_id="S10",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S10", verdict)
        assert verdict.regenerability.value == "REGENERABLE"
