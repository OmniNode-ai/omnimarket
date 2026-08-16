# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S1 + S2 — the Milestone-B business-path round trip (OMN-16004).

S1 is the delegation command leg (cloud -> local) and S2 is the terminal event
leg (local -> cloud): together they are "the same customer path back" that the
activation receipt's business-path binding item traverses. They are goldened in
one module because the property that matters is not two independent shape
matches — it is that **one** correlation id minted at the cloud publisher is
still the id the local runtime's terminal event carries.

Topic identity, per the registry rows: both legs are BARE canonical topics on
both sides. The registry is explicit that S1's consumer is NOT the
tenant-prefixing ``cloud_transport`` path (``node_bus_forwarder_effect`` is
confirmed not deployed to onex-dev), so the S1 goldens deliberately do not run
the prefix transform — that belongs to S4/S5/S6/S10, which drive the forwarder
directly.

What the round trip actually proves, and what it does not
---------------------------------------------------------

The first cut of this module contained a round-trip test that could not fail.
It read ``consumed.correlation_id`` off the parsed command and passed that
value straight into ``local_typed_envelope(...)`` to build the terminal event —
so the test itself performed the hop whose fidelity it claimed to be checking.
The failure mode named in its own docstring ("the runtime mints a fresh
correlation id at the local hop, silently severing the request from its own
terminal event") was structurally unreachable: no runtime was involved.

This module now drives the real carrier instead. The chain below is product
code at every hop the correlation id crosses:

* ``ServiceGatewayForwarder.consume_inbound_message`` takes the cloud wire
  bytes and republishes onto the bare local topic. The correlation id observed
  after that hop is re-parsed from the bytes the forwarder published.
* ``DispatcherDelegationRequest.handle`` consumes that published envelope and
  starts ``HandlerDelegationWorkflow``'s FSM, which is **keyed on the
  correlation id the command carried**.
* ``DispatcherQualityGateResult.handle`` emits the terminal event. It looks the
  workflow up by correlation id and, when no workflow matches, emits nothing at
  all — so a runtime that re-minted the id at the local hop produces **zero**
  terminal events rather than a mismatched one. That is the mechanism that
  makes the continuity claim falsifiable, and
  :meth:`TestRoundTripCorrelationContinuity.test_a_foreign_correlation_id_emits_no_terminal_event`
  is the negative control that exercises it.

Two things are explicitly NOT proven here, and are recorded as such in
``slice_manifest.yaml`` rather than papered over:

* **The runtime hop between the command and the gate result is not traversed.**
  Routing decision and inference response are supplied by this test, because
  the real ones require a routing reducer and a live LLM that unit CI does not
  have. The FSM transitions themselves are real; the stimuli are not.
* **Neither cloud endpoint is in this repo's dependency closure.** S1's
  producer is the cloud ``onex-api`` publisher and S2's consumer is the cloud
  terminal consumer. Both edges are therefore ``SHAPE_ONLY``, with the
  observable side asserted green and the unobservable side left unevaluated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_forwarder import (
    ServiceGatewayForwarder,
)

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
    DispatcherDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    DispatcherQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from tests.seam_goldens.harness import (
    GATEWAY_PRINCIPAL_ID,
    GATEWAY_TENANT_ID,
    GATEWAY_TENANT_SLUG,
    BusMessage,
    EnumSeamProjectionRole,
    RecordingEventBus,
    RecordingPublisher,
    assert_correlation_preserved,
    assert_registry_classification,
    assert_shape_only,
    build_forwarder_config,
    cloud_hand_rolled_envelope_json,
    consumer_projection,
    observed_projection_from_instance,
    omnimarket_node_event_bus,
    producer_projection,
    run_registry_match,
    wire_body,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

# The local runtime node that genuinely consumes the delegation command and
# emits the terminal events — resolved from its real contract below.
_LOCAL_RUNTIME_NODE = "node_delegation_orchestrator"

_S1_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_S2_COMPLETED_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_S2_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"
_S1_WIRE_TOPIC = f"tenant-{GATEWAY_TENANT_SLUG}.{_S1_TOPIC}"

# The key fields that actually cross the wire on the command leg.
# correlation_id is named explicitly because correlation continuity is the bar
# this ticket sets: a projection that omitted it could shape-match while the
# seam dropped it.
_ROUND_TRIP_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("envelope_id", "UUID"),
    ("correlation_id", "UUID"),
    ("event_type", "str"),
    ("payload", "dict[str, object]"),
)

# The terminal leg's field set differs from the command leg's by one field, and
# that difference is MEASURED, not stylistic: DispatcherQualityGateResult builds
# its envelope without an ``event_type`` (it routes by terminal class name, not
# by envelope event type) and publishes with ``exclude_none``, so the field is
# genuinely absent from the terminal wire body. Declaring it here would redden
# leg 2 for a reason that is not a defect. ``TestS2TerminalEventLeg
# .test_terminal_envelope_carries_no_event_type`` pins that absence so it stays
# a recorded fact rather than a silent omission from this tuple.
_TERMINAL_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("envelope_id", "UUID"),
    ("correlation_id", "UUID"),
    ("payload", "dict[str, object]"),
)

_TEST_ENDPOINT_URL = "http://delegation-llm.test:8000"


def _delegation_request_payload(correlation_id: UUID) -> dict[str, object]:
    """The command payload as the cloud publisher genuinely sends it.

    Carries ``correlation_id`` and ``emitted_at`` because the FSM key is the
    payload's correlation id: this is the field the local runtime actually
    threads the round trip on, so a golden that omitted it would be driving a
    different message than production does.
    """

    return {
        "prompt": "summarize the changelog",
        "task_type": "summarization",
        "correlation_id": str(correlation_id),
        "emitted_at": datetime.now(UTC).isoformat(),
    }


def _subscribed_command_topic() -> str:
    """The command topic as the CONSUMER's own contract.yaml declares it."""

    subscribed = [
        topic
        for topic in omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE).subscribe_topics
        if "delegation-request.v1" in topic
    ]
    if len(subscribed) != 1:
        raise AssertionError(
            f"{_LOCAL_RUNTIME_NODE} contract declares {len(subscribed)} "
            f"delegation-request subscriptions, expected exactly one: {subscribed}"
        )
    return subscribed[0]


async def _ingress_published_command(
    *, correlation_id: UUID, tmp_path: Path
) -> tuple[str, bytes]:
    """Run the REAL gateway ingress hop and return what it published locally.

    Cloud wire bytes in, ``ServiceGatewayForwarder`` runs its genuine decode /
    validate / transform / publish path, and the bytes it handed the local bus
    come back out. Everything downstream of here consumes product output, not
    test input.
    """

    local_bus = RecordingPublisher()
    forwarder = ServiceGatewayForwarder(
        config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
        local_bus=local_bus,
        cloud_bus=RecordingPublisher(),
    )
    await forwarder.consume_inbound_message(
        BusMessage(
            topic=_S1_WIRE_TOPIC,
            value=cloud_hand_rolled_envelope_json(
                envelope_id=uuid4(),
                correlation_id=correlation_id,
                event_type="omnibase-infra.delegation-request",
                payload=_delegation_request_payload(correlation_id),
                source_tenant_id=str(GATEWAY_TENANT_ID),
                source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
            ),
        )
    )
    published = local_bus.only()
    return published.topic, published.value


def _advance_workflow_to_gate(
    handler: HandlerDelegationWorkflow, correlation_id: UUID
) -> None:
    """Drive the real FSM from ROUTED to INFERENCE_COMPLETED.

    The transitions are the production ones; the routing decision and inference
    response feeding them are supplied here because unit CI has neither a
    routing reducer nor an LLM. That boundary is stated in the module docstring
    and recorded in the slice manifest — it is the one hop of the round trip
    this golden does not traverse.
    """

    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type="summarization",
            selected_model="qwen3-coder-30b",
            selected_backend_id=uuid5(
                NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
            ),
            endpoint_url=_TEST_ENDPOINT_URL,
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=65536,
            system_prompt="You are an assistant.",
            rationale="Routing fixed for the seam golden; no reducer in unit CI.",
        )
    )
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=correlation_id,
            content="the changelog summary",
            model_used="Qwen3-Coder-30B-A3B",
            llm_call_id="chatcmpl-seam-golden",
            latency_ms=100,
            prompt_tokens=50,
            completion_tokens=20,
            total_tokens=70,
        )
    )


def _gate_result_envelope(correlation_id: UUID) -> ModelEventEnvelope[object]:
    """The quality-gate verdict envelope the reducer publishes back."""

    return ModelEventEnvelope(
        envelope_id=uuid4(),
        correlation_id=correlation_id,
        payload=ModelQualityGateResult(
            correlation_id=correlation_id, passed=True, quality_score=0.9
        ),
        envelope_timestamp=datetime.now(UTC),
    )


async def _local_runtime_terminal_publishes(
    *,
    command_topic: str,
    command_bytes: bytes,
    gate_correlation_id: UUID,
) -> RecordingEventBus:
    """Run the REAL local runtime from consumed command to terminal publish.

    ``command_bytes`` are the bytes the gateway forwarder published, so the
    correlation id entering the FSM is one product code carried across the
    ingress hop — never one this test re-read and re-supplied.
    """

    consumed = ModelEventEnvelope[object].model_validate_json(command_bytes)
    assert command_topic == _subscribed_command_topic()

    handler = HandlerDelegationWorkflow()
    request_dispatch = await DispatcherDelegationRequest(handler).handle(consumed)
    if request_dispatch.status.value != "success":
        raise AssertionError(
            f"the real delegation-request dispatcher rejected the envelope the "
            f"gateway published: {request_dispatch.status.value} "
            f"{request_dispatch.error_message}"
        )
    assert request_dispatch.correlation_id is not None
    _advance_workflow_to_gate(handler, request_dispatch.correlation_id)

    bus = RecordingEventBus()
    await DispatcherQualityGateResult(handler, event_bus=bus).handle(  # type: ignore[arg-type]
        _gate_result_envelope(gate_correlation_id)
    )
    return bus


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
        assert TOPIC_ID_DELEGATION_REQUEST == _S1_TOPIC

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
            payload=_delegation_request_payload(correlation_id),
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

    def test_registry_match_is_shape_only_with_the_consumer_leg_observed(
        self,
    ) -> None:
        """S1 is honestly SHAPE_ONLY: its producer is not in this closure.

        The producing side is the cloud ``onex-api`` MSK publisher — not a
        Python package here — so the only "producer" available to project is
        the dict literal this suite wrote in ``harness``. Projecting that would
        compare the test to itself; the previous version of this call site did
        exactly that by passing ``observed_producer=declared_producer``, which
        made REGENERABLE unconditional. Leg 2 is left unevaluated instead.

        The CONSUMER leg is fully real and asserted green: the envelope model
        actually parses the bytes, the field presence check runs against the
        raw JSON body, and the topic is read out of
        ``node_delegation_orchestrator``'s own contract.yaml rather than
        restated from the declaration.
        """

        correlation_id = uuid4()
        wire_bytes = cloud_hand_rolled_envelope_json(
            envelope_id=uuid4(),
            correlation_id=correlation_id,
            event_type="omnibase-infra.delegation-request",
            payload=_delegation_request_payload(correlation_id),
            source_tenant_id=str(GATEWAY_TENANT_ID),
            source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
        )
        consumed = ModelEventEnvelope[dict[str, object]].model_validate_json(wire_bytes)

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
            observed_producer=None,
            observed_consumer=observed_projection_from_instance(
                edge_id="S1",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=_subscribed_command_topic(),
                instance=consumed,
                field_names=tuple(name for name, _ in _ROUND_TRIP_KEY_FIELDS),
                body=wire_body(wire_bytes),
            ),
        )

        assert_registry_classification("S1", verdict)
        assert_shape_only(
            "S1", verdict, producer_observed=False, consumer_observed=True
        )

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

    async def test_real_product_code_publishes_the_terminal_with_correlation(
        self, tmp_path: Path
    ) -> None:
        """The terminal event is emitted by the runtime, not built by this test.

        ``DispatcherQualityGateResult`` mints the envelope and chooses the
        topic; both come back off the recording bus. The correlation id
        asserted here entered the runtime through the gateway's published
        bytes.
        """

        correlation_id = uuid4()
        command_topic, command_bytes = await _ingress_published_command(
            correlation_id=correlation_id, tmp_path=tmp_path
        )
        bus = await _local_runtime_terminal_publishes(
            command_topic=command_topic,
            command_bytes=command_bytes,
            gate_correlation_id=correlation_id,
        )

        published = bus.only()
        assert published.topic == _S2_COMPLETED_TOPIC
        assert published.topic == TOPIC_ID_DELEGATION_COMPLETED
        assert_correlation_preserved(
            edge_id="S2",
            emitted=correlation_id,
            observed=published.reparsed().correlation_id,
        )

    async def test_terminal_envelope_carries_no_event_type(
        self, tmp_path: Path
    ) -> None:
        """Pin a measured fact the terminal projection depends on.

        ``DispatcherQualityGateResult`` routes by terminal CLASS name, not by
        envelope ``event_type``, and publishes with ``exclude_none`` — so the
        field is genuinely absent from the terminal wire body. That is why
        ``_TERMINAL_KEY_FIELDS`` omits it. Recording the absence here means a
        future runtime that starts populating ``event_type`` fails this test and
        forces the projection to be updated deliberately, instead of the
        omission quietly becoming folklore.
        """

        correlation_id = uuid4()
        command_topic, command_bytes = await _ingress_published_command(
            correlation_id=correlation_id, tmp_path=tmp_path
        )
        bus = await _local_runtime_terminal_publishes(
            command_topic=command_topic,
            command_bytes=command_bytes,
            gate_correlation_id=correlation_id,
        )

        published = bus.only()
        assert "event_type" not in wire_body(published.serialized())
        assert published.reparsed().event_type is None

    async def test_registry_match_is_shape_only_with_the_producer_leg_observed(
        self, tmp_path: Path
    ) -> None:
        """S2 is SHAPE_ONLY, but for the OPPOSITE reason to S1.

        Here the producing side is real product code in this repo and is
        genuinely observed: the projection is built from the bytes
        ``DispatcherQualityGateResult`` published, after a serialize / re-parse
        cycle, on the topic that dispatcher itself chose. (The first cut
        asserted this leg against ``harness.local_typed_envelope`` — an
        envelope the test constructed — and then passed the declaration in as
        the observation, so nothing about the runtime was checked at all.)

        The CONSUMING side is the cloud terminal consumer, which is not in this
        repo's dependency closure, so leg 3 is left unevaluated.
        """

        correlation_id = uuid4()
        command_topic, command_bytes = await _ingress_published_command(
            correlation_id=correlation_id, tmp_path=tmp_path
        )
        bus = await _local_runtime_terminal_publishes(
            command_topic=command_topic,
            command_bytes=command_bytes,
            gate_correlation_id=correlation_id,
        )
        published = bus.only()

        declared_producer = producer_projection(
            edge_id="S2",
            topic=_S2_COMPLETED_TOPIC,
            key_fields=_TERMINAL_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S2",
            topic=_S2_COMPLETED_TOPIC,
            key_fields=_TERMINAL_KEY_FIELDS,
        )

        verdict = run_registry_match(
            edge_id="S2",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_instance(
                edge_id="S2",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=published.topic,
                instance=published.reparsed(),
                field_names=tuple(name for name, _ in _TERMINAL_KEY_FIELDS),
                body=wire_body(published.serialized()),
            ),
            observed_consumer=None,
        )

        assert_registry_classification("S2", verdict)
        assert_shape_only(
            "S2", verdict, producer_observed=True, consumer_observed=False
        )


class TestRoundTripCorrelationContinuity:
    """The property neither leg proves alone: one id across both directions."""

    async def test_product_code_carries_one_correlation_id_command_to_terminal(
        self, tmp_path: Path
    ) -> None:
        """One id, minted once, observed at every hop product code performs.

        The id is minted here and then never re-supplied by this test between
        the hops: the gateway forwarder carries it from the cloud wire onto the
        local bare topic, the real delegation-request dispatcher lifts it off
        those published bytes into the FSM key, and the real quality-gate
        dispatcher stamps the terminal envelope with it. Every value asserted
        below is read back out of an artifact product code produced.
        """

        correlation_id = uuid4()

        # --- forward leg: cloud command through the REAL gateway ingress -----
        command_topic, command_bytes = await _ingress_published_command(
            correlation_id=correlation_id, tmp_path=tmp_path
        )
        consumed = ModelEventEnvelope[dict[str, object]].model_validate_json(
            command_bytes
        )
        assert command_topic == _subscribed_command_topic()
        assert_correlation_preserved(
            edge_id="S1", emitted=correlation_id, observed=consumed.correlation_id
        )

        # --- local processing: the REAL runtime, keyed on the carried id -----
        bus = await _local_runtime_terminal_publishes(
            command_topic=command_topic,
            command_bytes=command_bytes,
            gate_correlation_id=correlation_id,
        )

        # --- return leg: the terminal the runtime actually published ---------
        published = bus.only()
        assert published.topic == _S2_COMPLETED_TOPIC
        terminal = published.reparsed()
        assert_correlation_preserved(
            edge_id="S2", emitted=correlation_id, observed=terminal.correlation_id
        )

        # The terminal PAYLOAD carries it too — an envelope-only match would
        # leave the projection join (which reads payload.correlation_id) broken
        # while the envelope looked healthy.
        assert isinstance(terminal.payload, dict)
        assert terminal.payload["correlation_id"] == str(correlation_id)

    async def test_a_foreign_correlation_id_emits_no_terminal_event(
        self, tmp_path: Path
    ) -> None:
        """The negative control that makes the continuity claim falsifiable.

        This is the exact failure the round trip claims to catch: the runtime
        losing the command's correlation id at the local hop. Driving the gate
        result under a DIFFERENT id finds no workflow in the FSM, so the real
        dispatcher publishes **nothing** — no terminal event is severed from
        its request, because none is emitted at all.

        The old version of this test could not reach this state: it read the
        id off the consumed envelope and passed it into the terminal
        constructor itself, so continuity held by construction no matter what
        the runtime did.
        """

        correlation_id = uuid4()
        command_topic, command_bytes = await _ingress_published_command(
            correlation_id=correlation_id, tmp_path=tmp_path
        )

        bus = await _local_runtime_terminal_publishes(
            command_topic=command_topic,
            command_bytes=command_bytes,
            gate_correlation_id=uuid4(),
        )

        assert bus.published == [], (
            "a quality-gate verdict under a correlation id the command never "
            "started must not produce a terminal event; the FSM key is the "
            "continuity mechanism and it is not holding"
        )

    def test_command_and_terminal_topics_are_disjoint_but_same_runtime(self) -> None:
        """The round trip is two topics on one node, not one topic echoed back."""

        event_bus = omnimarket_node_event_bus(_LOCAL_RUNTIME_NODE)
        assert _S1_TOPIC in event_bus.subscribe_topics
        assert _S1_TOPIC not in event_bus.publish_topics
        assert _S2_COMPLETED_TOPIC in event_bus.publish_topics
        assert _S2_COMPLETED_TOPIC not in event_bus.subscribe_topics
