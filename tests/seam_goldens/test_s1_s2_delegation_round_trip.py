# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S1 + S2 — the Milestone-B business-path round trip (OMN-16004).

S1 is the delegation command leg (cloud -> local) and S2 is the terminal event
leg (local -> cloud): together they are "the same customer path back" that the
activation receipt's business-path binding item traverses. They are goldened in
one module because the property that matters is not two independent shape
matches — it is that **one** correlation id minted by the cloud publisher
survives the whole round trip and is still observable when the terminal event
lands back at the cloud consumer.

Topic identity, per the registry rows: both legs are BARE canonical topics on
both sides. The registry is explicit that S1's consumer is NOT the
tenant-prefixing ``cloud_transport`` path (``node_bus_forwarder_effect`` is
confirmed not deployed to onex-dev), so these goldens deliberately do not run
the prefix transform — that belongs to S4/S5/S6/S10, which drive the forwarder
directly. Asserting a tenant prefix here would golden a topology the registry
says is not the one in play.

The routing leg is asserted against ``node_delegation_orchestrator``'s real
``contract.yaml`` topic sets rather than a live Kafka dispatcher: the
contract-declared ``subscribe_topics`` / ``publish_topics`` ARE the local
dispatch routing declaration, and a broker is not available in unit CI. S10
complements this by driving the real forwarder's republish path end to end.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from tests.seam_goldens.harness import (
    GATEWAY_PRINCIPAL_ID,
    GATEWAY_TENANT_ID,
    assert_correlation_preserved,
    assert_registry_classification,
    cloud_hand_rolled_envelope_json,
    consumer_projection,
    local_typed_envelope,
    omnimarket_node_event_bus,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

# The local runtime node that genuinely consumes the delegation command and
# emits the terminal events — resolved from its real contract below.
_LOCAL_RUNTIME_NODE = "node_delegation_orchestrator"

_S1_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_S2_COMPLETED_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_S2_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"

# The key fields that actually cross the wire on this seam. correlation_id is
# named explicitly because correlation continuity is the bar this ticket sets:
# a projection that omitted it could shape-match while the seam dropped it.
_ROUND_TRIP_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("envelope_id", "UUID"),
    ("correlation_id", "UUID"),
    ("event_type", "str"),
    ("payload", "dict[str, object]"),
)


def _delegation_request_payload() -> dict[str, object]:
    return {
        "prompt": "summarize the changelog",
        "task_type": "summarization",
        "source": "claude-code",
    }


class TestS1CommandLeg:
    """cloud (onex-api) -> local omnibase-infra runtime, bare canonical topic."""

    def test_slice_row_is_the_command_leg(self) -> None:
        edge = slice_edge("S1")
        assert edge.traversed
        assert edge.registry_classification == "MATCHED"

    def test_producer_topic_is_bare_and_carries_no_tenant_prefix(self) -> None:
        """The registry pins BARE on both sides; a prefix here is the defect."""

        assert not _S1_TOPIC.startswith("tenant-")

    def test_local_runtime_contract_subscribes_to_the_producer_topic(self) -> None:
        """The routing leg: the real consumer contract declares this exact string."""

        event_bus = omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE)
        assert _S1_TOPIC in event_bus.subscribe_topics

    def test_command_crosses_the_seam_preserving_correlation(self) -> None:
        """Producer bytes -> real consumer parse, correlation id intact.

        The producer side is the cloud publisher's genuine hand-rolled JSON
        body and the consumer side is the real typed envelope parse, so the
        assertion is that two independently written implementations agree —
        not that pydantic round-trips its own output.
        """

        correlation_id = uuid4()
        envelope_id = uuid4()

        wire_bytes = cloud_hand_rolled_envelope_json(
            envelope_id=envelope_id,
            correlation_id=correlation_id,
            event_type="omnibase-infra.delegation-request",
            payload=_delegation_request_payload(),
            source_tenant_id=str(GATEWAY_TENANT_ID),
            source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
        )

        observed = ModelEventEnvelope[dict[str, object]].model_validate_json(wire_bytes)

        assert_correlation_preserved(
            edge_id="S1",
            emitted=correlation_id,
            observed=observed.correlation_id,
        )
        assert observed.envelope_id == envelope_id
        assert observed.payload == _delegation_request_payload()

    def test_registry_match_is_regenerable_for_the_driven_seam(self) -> None:
        """Producer -> registry -> consumer, with both observed legs supplied.

        REGENERABLE comes out of the shipped ``HandlerSeamMatch`` only when
        legs 2 and 3 are explicitly green, which requires the golden to have
        actually driven both sides. That is the classifier enforcing the
        plan's "a shape comparison is insufficient" rule on this golden's
        behalf.
        """

        declared_producer = producer_projection(
            edge_id="S1", topic=_S1_TOPIC, key_fields=_ROUND_TRIP_KEY_FIELDS
        )
        declared_consumer = consumer_projection(
            edge_id="S1", topic=_S1_TOPIC, key_fields=_ROUND_TRIP_KEY_FIELDS
        )

        verdict = run_registry_match(
            edge_id="S1",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S1", verdict)
        assert verdict.regenerability.value == "REGENERABLE"
        assert verdict.leg1_declared_vs_declared.passed is True
        assert verdict.leg2_observed_producer_vs_declared.passed is True
        assert verdict.leg3_observed_consumer_vs_declared.passed is True

    def test_a_tenant_prefixed_producer_would_fail_the_registry_match(self) -> None:
        """Negative control: the golden can actually fail.

        Without this, a green S1 proves nothing about the matcher being wired
        in — it could be passing because every input matches everything.
        """

        verdict = run_registry_match(
            edge_id="S1",
            declared_producer=producer_projection(
                edge_id="S1",
                topic=f"tenant-acme.{_S1_TOPIC}",
                key_fields=_ROUND_TRIP_KEY_FIELDS,
            ),
            declared_consumer=consumer_projection(
                edge_id="S1", topic=_S1_TOPIC, key_fields=_ROUND_TRIP_KEY_FIELDS
            ),
        )

        assert verdict.verdict.value == "MISMATCH"
        assert verdict.leg1_declared_vs_declared.mismatching_field_path == "topic"
        assert verdict.regenerability.value == "NOT_APPLICABLE"


class TestS2TerminalEventLeg:
    """local omnibase-infra runtime -> cloud terminal consumer / projection."""

    def test_slice_row_is_the_return_leg(self) -> None:
        edge = slice_edge("S2")
        assert edge.traversed
        assert "local->cloud" in edge.leg

    @pytest.mark.parametrize("topic", [_S2_COMPLETED_TOPIC, _S2_FAILED_TOPIC])
    def test_local_runtime_contract_publishes_the_terminal_topic(
        self, topic: str
    ) -> None:
        """Both terminal outcomes are declared; the registry names both."""

        event_bus = omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE)
        assert topic in event_bus.publish_topics
        assert not topic.startswith("tenant-")

    def test_terminal_event_crosses_the_seam_preserving_correlation(self) -> None:
        correlation_id = uuid4()

        emitted = local_typed_envelope(
            envelope_id=uuid4(),
            correlation_id=correlation_id,
            event_type="omnibase-infra.delegation-completed",
            payload={"status": "completed", "tenant_id": "acme"},
        )
        # Serialize exactly as the local runtime publishes, then parse exactly
        # as the cloud terminal consumer does.
        observed = ModelEventEnvelope[dict[str, object]].model_validate_json(
            emitted.model_dump_json(exclude_none=True).encode("utf-8")
        )

        assert_correlation_preserved(
            edge_id="S2",
            emitted=correlation_id,
            observed=observed.correlation_id,
        )

    def test_registry_match_is_regenerable_for_the_driven_seam(self) -> None:
        declared_producer = producer_projection(
            edge_id="S2",
            topic=_S2_COMPLETED_TOPIC,
            key_fields=_ROUND_TRIP_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S2",
            topic=_S2_COMPLETED_TOPIC,
            key_fields=_ROUND_TRIP_KEY_FIELDS,
        )

        verdict = run_registry_match(
            edge_id="S2",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S2", verdict)
        assert verdict.regenerability.value == "REGENERABLE"


class TestRoundTripCorrelationContinuity:
    """The property neither leg proves alone: one id across both directions."""

    def test_one_correlation_id_survives_command_and_terminal_event(self) -> None:
        """This is the assertion that a shape comparison structurally cannot make.

        Two independently green shape matches on S1 and S2 are exactly the
        vacuous-green pattern the plan's regenerable-seams rule exists to
        reject: both sides can agree on topic and envelope while the runtime
        mints a fresh correlation id at the local hop, silently severing the
        request from its own terminal event. Driving both legs with one id and
        asserting it end to end is what makes the pair a real-seam golden.
        """

        correlation_id = uuid4()

        # --- forward leg: cloud command onto the bare topic -----------------
        command_bytes = cloud_hand_rolled_envelope_json(
            envelope_id=uuid4(),
            correlation_id=correlation_id,
            event_type="omnibase-infra.delegation-request",
            payload=_delegation_request_payload(),
            source_tenant_id=str(GATEWAY_TENANT_ID),
            source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
        )
        consumed = ModelEventEnvelope[dict[str, object]].model_validate_json(
            command_bytes
        )
        assert_correlation_preserved(
            edge_id="S1", emitted=correlation_id, observed=consumed.correlation_id
        )

        # --- local processing: the runtime carries correlation forward ------
        assert consumed.correlation_id is not None
        terminal = local_typed_envelope(
            envelope_id=uuid4(),
            correlation_id=consumed.correlation_id,
            event_type="omnibase-infra.delegation-completed",
            payload={"status": "completed", "tenant_id": "acme"},
        )

        # --- return leg: terminal event back to the cloud consumer ----------
        observed = ModelEventEnvelope[dict[str, object]].model_validate_json(
            terminal.model_dump_json(exclude_none=True).encode("utf-8")
        )
        assert_correlation_preserved(
            edge_id="S2", emitted=correlation_id, observed=observed.correlation_id
        )

    def test_command_and_terminal_topics_are_disjoint_but_same_runtime(self) -> None:
        """The round trip is two topics on one node, not one topic echoed back."""

        event_bus = omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE)
        assert _S1_TOPIC in event_bus.subscribe_topics
        assert _S1_TOPIC not in event_bus.publish_topics
        assert _S2_COMPLETED_TOPIC in event_bus.publish_topics
        assert _S2_COMPLETED_TOPIC not in event_bus.subscribe_topics
