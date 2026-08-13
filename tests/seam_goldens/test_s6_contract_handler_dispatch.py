# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S6 — the contract -> handler seam at the ingress/bridge, both directions.

S6 is the component the Milestone-B hop sequence names as the ingress/bridge,
and the receipt's round trip traverses it twice: ``gateway.consume_inbound`` on
the way in and ``gateway.forward_outbound`` on the way back. Both handlers are
driven through their real dispatch entrypoint — ``handle()`` with the outer
envelope delivered as a raw ``dict``, which is the shape the runtime dispatcher
genuinely passes (the OMN-13580 coercion path) rather than the convenience
typed form.

Correlation preservation here is not incidental: ``handle()`` returns a
``ModelHandlerOutput`` whose ``correlation_id`` is contractually "copied from
the input envelope", so the goldens assert that copy actually happens in both
directions.

A note on what this golden deliberately does NOT assert. The registry records
S6's consumer shape as ``ServiceGatewayForwarder._prepare_outbound`` /
``_prepare_inbound`` delegating to these contract-declared handlers. In the
``omnibase_infra`` version this repo actually pins, that delegation is not
present — the service re-derives the tenant-prefix transform locally and never
references either handler class. Asserting the registry's claim would fail;
asserting its negation would bless the non-delegation as correct. So the
golden asserts the property that must hold either way and that catches the
real risk: the two implementations of the transform must agree byte for byte.
If infra later lands the delegation, this parity holds trivially. If they
drift, the seam breaks here instead of in production.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.nodes.node_bus_forwarder_effect.handlers.handler_consume_inbound import (
    HandlerConsumeInbound,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.handlers.handler_forward_outbound import (
    HandlerForwardOutbound,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_envelope import (
    ModelGatewayEnvelope,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_forwarder_config import (
    ModelGatewayForwarderConfig,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.services import (
    service_gateway_forwarder as forwarder_module,
)
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
    load_gateway_contract,
    local_typed_envelope,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_INBOUND_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_OUTBOUND_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_INBOUND_WIRE = f"tenant-{GATEWAY_TENANT_SLUG}.{_INBOUND_TOPIC}"
_OUTBOUND_WIRE = f"tenant-{GATEWAY_TENANT_SLUG}.{_OUTBOUND_TOPIC}"

_S6_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("tenant_id", "UUID"),
    ("tenant_slug", "str"),
    ("envelope_id", "UUID"),
    ("correlation_id", "UUID"),
    ("canonical_topic", "str"),
    ("wire_topic", "str"),
)


@pytest.fixture
def config(tmp_path: Path) -> ModelGatewayForwarderConfig:
    return build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite")


def _gateway_envelope(
    *,
    correlation_id: UUID,
    canonical_topic: str,
    wire_topic: str,
    source_topic: str,
    payload: dict[str, object] | None = None,
) -> ModelGatewayEnvelope:
    return ModelGatewayEnvelope(
        tenant_id=GATEWAY_TENANT_ID,
        tenant_slug=GATEWAY_TENANT_SLUG,
        envelope_id=uuid4(),
        correlation_id=correlation_id,
        event_type="omnibase-infra.delegation",
        source_topic=source_topic,
        wire_topic=wire_topic,
        canonical_topic=canonical_topic,
        payload=payload or {"prompt": "p", "task_type": "summarization"},
    )


def _dispatched_dict(gateway_envelope: ModelGatewayEnvelope) -> dict[str, object]:
    """The raw outer-envelope dict the runtime dispatcher actually delivers."""

    outer = ModelEventEnvelope[dict[str, object]](
        envelope_id=uuid4(),
        correlation_id=gateway_envelope.correlation_id,
        event_type=gateway_envelope.event_type,
        payload=gateway_envelope.model_dump(mode="json"),
    )
    dumped = outer.model_dump(mode="json")
    if not isinstance(dumped, dict):  # pragma: no cover - defensive
        raise TypeError("outer envelope did not dump to a mapping")
    return dumped


class TestContractDeclaresBothOperations:
    """The producer side of S6 is the packaged contract's handler_routing."""

    def test_slice_row_covers_both_directions(self) -> None:
        edge = slice_edge("S6")
        assert edge.traversed
        assert "both directions" in edge.leg

    def test_contract_declares_the_two_gateway_operations(self) -> None:
        routing = load_gateway_contract()["handler_routing"]
        assert isinstance(routing, dict)
        handlers = routing["handlers"]
        assert isinstance(handlers, list)
        operations = {
            str(entry["operation"]) for entry in handlers if isinstance(entry, dict)
        }
        assert operations == {"gateway.forward_outbound", "gateway.consume_inbound"}

    def test_contract_names_the_classes_that_actually_exist(self) -> None:
        """The declared handler classes must be importable under those names.

        A contract naming a class that no longer exists is the cheapest
        possible seam break and the one a per-side unit suite never sees.
        """

        routing = load_gateway_contract()["handler_routing"]
        assert isinstance(routing, dict)
        handlers = routing["handlers"]
        assert isinstance(handlers, list)
        declared = {
            str(entry["operation"]): entry["handler"]
            for entry in handlers
            if isinstance(entry, dict)
        }

        outbound = declared["gateway.forward_outbound"]
        inbound = declared["gateway.consume_inbound"]
        assert isinstance(outbound, dict)
        assert isinstance(inbound, dict)
        assert outbound["name"] == HandlerForwardOutbound.__name__
        assert inbound["name"] == HandlerConsumeInbound.__name__
        assert outbound["module"] == HandlerForwardOutbound.__module__
        assert inbound["module"] == HandlerConsumeInbound.__module__

    def test_contract_io_model_is_the_class_the_handlers_transform(self) -> None:
        contract = load_gateway_contract()
        input_model = contract["input_model"]
        output_model = contract["output_model"]
        assert isinstance(input_model, dict)
        assert isinstance(output_model, dict)
        assert input_model["name"] == ModelGatewayEnvelope.__name__
        assert output_model["name"] == ModelGatewayEnvelope.__name__


class TestConsumeInboundDirection:
    """cloud wire topic -> bare canonical topic, through the real handler."""

    async def test_dispatch_entrypoint_strips_prefix_and_keeps_correlation(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        correlation_id = uuid4()
        envelope = _gateway_envelope(
            correlation_id=correlation_id,
            canonical_topic=_INBOUND_TOPIC,
            wire_topic=_INBOUND_WIRE,
            source_topic=_INBOUND_WIRE,
        )

        output = await HandlerConsumeInbound(config).handle(_dispatched_dict(envelope))

        assert output.result is not None
        assert output.result.canonical_topic == _INBOUND_TOPIC
        assert output.result.source_topic == _INBOUND_WIRE
        assert_correlation_preserved(
            edge_id="S6", emitted=correlation_id, observed=output.correlation_id
        )

    async def test_undeclared_canonical_topic_is_rejected(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        """Only contract-declared mirror topics may cross the trust boundary."""

        undeclared = "onex.cmd.omnibase-infra.not-declared-anywhere.v1"
        envelope = _gateway_envelope(
            correlation_id=uuid4(),
            canonical_topic=undeclared,
            wire_topic=f"tenant-{GATEWAY_TENANT_SLUG}.{undeclared}",
            source_topic=f"tenant-{GATEWAY_TENANT_SLUG}.{undeclared}",
        )

        with pytest.raises(ValueError, match="not declared for inbound mirroring"):
            await HandlerConsumeInbound(config).handle(_dispatched_dict(envelope))


class TestForwardOutboundDirection:
    """bare canonical topic -> tenant wire topic, through the real handler."""

    async def test_dispatch_entrypoint_adds_prefix_and_keeps_correlation(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        correlation_id = uuid4()
        envelope = _gateway_envelope(
            correlation_id=correlation_id,
            canonical_topic=_OUTBOUND_TOPIC,
            wire_topic=_OUTBOUND_WIRE,
            source_topic=_OUTBOUND_TOPIC,
            payload={"status": "completed"},
        )

        output = await HandlerForwardOutbound(config).handle(_dispatched_dict(envelope))

        assert output.result is not None
        assert output.result.wire_topic == _OUTBOUND_WIRE
        assert output.result.source_topic == _OUTBOUND_TOPIC
        assert_correlation_preserved(
            edge_id="S6", emitted=correlation_id, observed=output.correlation_id
        )

    async def test_envelope_tenant_must_match_the_attached_tenant(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        envelope = _gateway_envelope(
            correlation_id=uuid4(),
            canonical_topic=_OUTBOUND_TOPIC,
            wire_topic=_OUTBOUND_WIRE,
            source_topic=_OUTBOUND_TOPIC,
        ).model_copy(update={"tenant_id": uuid4()})

        with pytest.raises(ValueError, match="tenant_id does not match"):
            await HandlerForwardOutbound(config).handle(_dispatched_dict(envelope))


class TestHandlerAndLiveServiceAgree:
    """The parity assertion that survives either delegation state.

    See the module docstring: the registry claims the live service delegates to
    these handlers, and the pinned wheel does not. Rather than encode either
    claim, these goldens pin that the two transform implementations produce the
    same wire result — the property whose violation is the actual outage.
    """

    def test_pinned_infra_does_not_yet_delegate_to_the_contract_handlers(
        self,
    ) -> None:
        """Pin the measured divergence between the registry row and the wheel.

        The registry's S6 ``consumer_shape`` states that
        ``ServiceGatewayForwarder._prepare_outbound`` / ``_prepare_inbound``
        "now DELEGATE the tenant-prefix transform to the contract-declared
        HandlerForwardOutbound/HandlerConsumeInbound COMPUTE handlers rather
        than re-deriving it locally". Measured against the ``omnibase_infra``
        version this repo pins, that is not true: the service module never
        references either handler class.

        This is a pin on observed reality, not an endorsement of it. When infra
        lands the delegation, this test FAILS — and that failure is the
        intended signal: ``seams.v1.yaml`` must be re-derived against the new
        topology and this pin deleted. Recording it as a failing-on-change
        assertion is what stops the registry's claim from quietly staying
        wrong.
        """

        source = inspect.getsource(forwarder_module)
        assert "HandlerForwardOutbound" not in source
        assert "HandlerConsumeInbound" not in source

    async def test_inbound_transform_parity(self, tmp_path: Path) -> None:
        """Handler and live service must strip to the same canonical topic
        and stamp the same verified tenant into the payload."""

        config = build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite")
        correlation_id = uuid4()
        payload: dict[str, object] = {
            "prompt": "summarize the changelog",
            "task_type": "summarization",
            "source": "claude-code",
        }

        handler_output = await HandlerConsumeInbound(config).handle(
            _dispatched_dict(
                _gateway_envelope(
                    correlation_id=correlation_id,
                    canonical_topic=_INBOUND_TOPIC,
                    wire_topic=_INBOUND_WIRE,
                    source_topic=_INBOUND_WIRE,
                    payload=dict(payload),
                )
            )
        )

        local_bus = RecordingPublisher()
        service = ServiceGatewayForwarder(
            config=config, local_bus=local_bus, cloud_bus=RecordingPublisher()
        )
        await service.consume_inbound_message(
            BusMessage(
                topic=_INBOUND_WIRE,
                value=cloud_hand_rolled_envelope_json(
                    envelope_id=uuid4(),
                    correlation_id=correlation_id,
                    event_type="omnibase-infra.delegation-request",
                    payload=dict(payload),
                    source_tenant_id=str(GATEWAY_TENANT_ID),
                    source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
                ),
            )
        )

        service_published = local_bus.only()
        assert handler_output.result is not None
        assert handler_output.result.canonical_topic == service_published.topic
        assert (
            handler_output.result.payload["tenant_id"]
            == service_published.envelope().payload["tenant_id"]
            == GATEWAY_TENANT_SLUG
        )

    async def test_outbound_transform_parity(self, tmp_path: Path) -> None:
        """Handler and live service must produce the same tenant wire topic."""

        config = build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite")
        correlation_id = uuid4()

        handler_output = await HandlerForwardOutbound(config).handle(
            _dispatched_dict(
                _gateway_envelope(
                    correlation_id=correlation_id,
                    canonical_topic=_OUTBOUND_TOPIC,
                    wire_topic=_OUTBOUND_WIRE,
                    source_topic=_OUTBOUND_TOPIC,
                    payload={"status": "completed"},
                )
            )
        )

        cloud_bus = RecordingPublisher()
        service = ServiceGatewayForwarder(
            config=config, local_bus=RecordingPublisher(), cloud_bus=cloud_bus
        )
        await service.forward_outbound_message(
            BusMessage(
                topic=_OUTBOUND_TOPIC,
                value=local_typed_envelope(
                    envelope_id=uuid4(),
                    correlation_id=correlation_id,
                    event_type="omnibase-infra.delegation-completed",
                    payload={"status": "completed"},
                )
                .model_dump_json(exclude_none=True)
                .encode("utf-8"),
            )
        )

        service_published = cloud_bus.only()
        assert handler_output.result is not None
        assert handler_output.result.wire_topic == service_published.topic
        assert_correlation_preserved(
            edge_id="S6",
            emitted=correlation_id,
            observed=service_published.envelope().correlation_id,
        )


class TestS6RegistryMatch:
    def test_registry_match_is_regenerable_for_the_driven_handlers(self) -> None:
        declared_producer = producer_projection(
            edge_id="S6",
            topic=_INBOUND_TOPIC,
            envelope_model=(
                "omnibase_infra.nodes.node_bus_forwarder_effect.models."
                "model_gateway_envelope.ModelGatewayEnvelope"
            ),
            envelope_version="0.1.0",
            key_fields=_S6_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S6",
            topic=_INBOUND_TOPIC,
            envelope_model=(
                "omnibase_infra.nodes.node_bus_forwarder_effect.models."
                "model_gateway_envelope.ModelGatewayEnvelope"
            ),
            envelope_version="0.1.0",
            key_fields=_S6_KEY_FIELDS,
        )

        verdict = run_registry_match(
            edge_id="S6",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S6", verdict)
        assert verdict.regenerability.value == "REGENERABLE"
