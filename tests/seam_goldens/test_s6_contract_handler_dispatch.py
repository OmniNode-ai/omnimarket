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

A note on the consumer side, re-scored at the pin (OMN-16077). The registry
records S6's consumer shape as ``ServiceGatewayForwarder._prepare_outbound`` /
``_prepare_inbound`` delegating to these contract-declared handlers. At the
``omnibase_infra`` rev this repo now resolves — ``94247acff``, which has the
OMN-15740 delegation commit ``642e60a87`` as an ancestor — that delegation is
real: the executing service imports and constructs both handler classes and
routes the tenant-prefix transform through them. This restores the pre-
OMN-16033 scoring, whose ``UNMATCHED`` verdict was specific to the earlier
``b6bd79c34`` pin that predated the delegation. The golden still asserts the
transform-parity property directly, because it is the property whose
violation is the outage regardless of which implementation runs.

S6 is one of the five edges that classify ``REGENERABLE``, and it earns that
from two genuinely independent artifacts. The observed PRODUCER is read out of
the ``contract.yaml`` packaged inside the pinned wheel — its declared
``input_model`` is resolved by import and its field types come from that
class's own annotations, on the mirror topic that same file declares. The
observed CONSUMER is the ``ModelGatewayEnvelope`` ``HandlerConsumeInbound``
actually returned from a real dispatch. A contract that renames its IO model
reddens leg 2; a handler that stops stripping the prefix reddens leg 3.
"""

from __future__ import annotations

import importlib
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
    UNVERSIONED_MODEL,
    BusMessage,
    EnumSeamProjectionRole,
    RecordingPublisher,
    assert_correlation_preserved,
    assert_regenerable,
    assert_registry_classification,
    build_forwarder_config,
    cloud_hand_rolled_envelope_json,
    consumer_projection,
    gateway_contract_version,
    gateway_mirror_topics,
    load_gateway_contract,
    local_typed_envelope,
    model_identity,
    observed_projection_from_instance,
    observed_projection_from_model_class,
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

# ``ModelGatewayEnvelope`` declares no wire version field. The projection
# records that (harness.UNVERSIONED_MODEL) instead of a fabricated semver; the
# packaged contract's own ``contract_version`` is pinned separately below,
# where it is a real assertion rather than a decoration on a projection.
_S6_DECLARED_MODEL = (
    "omnibase_infra.nodes.node_bus_forwarder_effect.models."
    "model_gateway_envelope.ModelGatewayEnvelope"
)
# 0.1.0 -> 0.1.1: the packaged contract gained the contract-declared canary
# probe block (OMN-15741) in the 0.38.6 wheel this repo pins as of OMN-16077.
_EXPECTED_CONTRACT_VERSION = "0.1.1"


def _contract_declared_input_model() -> type[object]:
    """Import the IO model class the PACKAGED contract names.

    The S6 producer is a declaration, not a message, so this is what observing
    it means: read ``input_model.module`` / ``input_model.name`` out of the
    contract shipped in the wheel and resolve them. A contract that renames its
    IO model, or names a class that no longer exists, fails right here rather
    than at runtime.
    """

    input_model = load_gateway_contract()["input_model"]
    if not isinstance(input_model, dict):
        raise TypeError("gateway contract input_model block is not a mapping")
    module = importlib.import_module(str(input_model["module"]))
    resolved = getattr(module, str(input_model["name"]))
    if not isinstance(resolved, type):
        raise TypeError(
            f"gateway contract input_model {input_model['name']!r} did not "
            f"resolve to a class"
        )
    return resolved


def _contract_declared_inbound_topic() -> str:
    """The inbound mirror topic string, read from the packaged contract."""

    declared = [
        topic
        for topic in gateway_mirror_topics().inbound
        if "delegation-request.v1" in topic
    ]
    if len(declared) != 1:
        raise AssertionError(
            f"packaged gateway contract declares {len(declared)} inbound "
            f"delegation-request mirror topics, expected exactly one: {declared}"
        )
    return str(declared[0])


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
    """The parity assertion, plus the delegation pin that keeps the row honest.

    See the module docstring: at the pinned rev the registry's claim that the
    live service delegates to these handlers is TRUE, and the pin below
    measures it. The parity goldens still pin that the two transform
    implementations produce the same wire result — the property whose
    violation is the actual outage.
    """

    async def test_pinned_infra_delegates_to_the_contract_handlers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the agreement between the registry row and the wheel.

        The registry's S6 ``consumer_shape`` states that
        ``ServiceGatewayForwarder._prepare_outbound`` / ``_prepare_inbound``
        "now DELEGATE the tenant-prefix transform to the contract-declared
        HandlerForwardOutbound/HandlerConsumeInbound COMPUTE handlers rather
        than re-deriving it locally". Measured against the ``omnibase_infra``
        rev this repo pins as of OMN-16077 (``94247acff``, which contains the
        OMN-15740 delegation commit ``642e60a87``), that is true — and this
        pin measures the INVOCATION, not name presence: spy subclasses are
        patched over the handler classes in the service module's namespace,
        both live entrypoints are driven with real bus messages, and each spy
        must record a call to its transform method (``forward_outbound`` /
        ``consume_inbound`` — the delegation seam the service actually
        invokes). A service that reverted to a local re-implementation
        (leaving the imports as dead references) fails here, which a
        source-text substring check could not catch.

        This inverts the ``does_not_yet_delegate`` pin that guarded the
        pre-delegation pins (``b6bd79c34`` and earlier), exactly as that pin's
        docstring prescribed when it fired. If a future pin move drops the
        delegation again, this fails — and the registry row must be re-scored
        before this assertion is touched.
        """

        invoked: list[str] = []

        class SpyForwardOutbound(HandlerForwardOutbound):
            def forward_outbound(self, *args: object, **kwargs: object) -> object:
                invoked.append("outbound")
                return super().forward_outbound(*args, **kwargs)

        class SpyConsumeInbound(HandlerConsumeInbound):
            def consume_inbound(self, *args: object, **kwargs: object) -> object:
                invoked.append("inbound")
                return super().consume_inbound(*args, **kwargs)

        monkeypatch.setattr(
            forwarder_module, "HandlerForwardOutbound", SpyForwardOutbound
        )
        monkeypatch.setattr(
            forwarder_module, "HandlerConsumeInbound", SpyConsumeInbound
        )

        config = build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite")
        service = ServiceGatewayForwarder(
            config=config,
            local_bus=RecordingPublisher(),
            cloud_bus=RecordingPublisher(),
        )

        await service.forward_outbound_message(
            BusMessage(
                topic=_OUTBOUND_TOPIC,
                value=local_typed_envelope(
                    envelope_id=uuid4(),
                    correlation_id=uuid4(),
                    event_type="omnibase-infra.delegation-completed",
                    payload={"status": "completed"},
                )
                .model_dump_json(exclude_none=True)
                .encode("utf-8"),
            )
        )
        await service.consume_inbound_message(
            BusMessage(
                topic=_INBOUND_WIRE,
                value=cloud_hand_rolled_envelope_json(
                    envelope_id=uuid4(),
                    correlation_id=uuid4(),
                    event_type="omnibase-infra.delegation-request",
                    payload={"prompt": "ping", "task_type": "summarization"},
                    source_tenant_id=str(GATEWAY_TENANT_ID),
                    source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
                ),
            )
        )

        assert invoked == ["outbound", "inbound"]

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
    def test_packaged_contract_version_is_pinned(self) -> None:
        """A wheel bump that moves the contract version is a decision.

        Kept as its own assertion rather than folded into the projection: the
        gateway envelope carries no wire version, so putting a semver on the
        projection would have been decoration. Here it is a real check against
        the packaged file.
        """

        assert gateway_contract_version() == _EXPECTED_CONTRACT_VERSION

    async def test_registry_match_is_regenerable_for_the_driven_handlers(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        """Producer observed from the packaged contract; consumer from a real run.

        PRODUCER: the ``input_model`` class the contract inside the pinned
        omnibase_infra wheel names, resolved by import, with its field types
        read off the class's own annotations, on the mirror topic that same
        contract declares. Nothing here is authored by this test.

        CONSUMER: the ``ModelGatewayEnvelope`` that ``HandlerConsumeInbound``
        actually returned when driven through its real dispatch entrypoint, on
        the canonical topic that handler actually produced.

        The two are derived from different artifacts — a YAML declaration and a
        handler return value — so a handler that stopped stripping the prefix,
        or a contract that renamed its IO model, breaks exactly one leg.
        """

        correlation_id = uuid4()
        output = await HandlerConsumeInbound(config).handle(
            _dispatched_dict(
                _gateway_envelope(
                    correlation_id=correlation_id,
                    canonical_topic=_INBOUND_TOPIC,
                    wire_topic=_INBOUND_WIRE,
                    source_topic=_INBOUND_WIRE,
                )
            )
        )
        assert output.result is not None

        declared_producer = producer_projection(
            edge_id="S6",
            topic=_INBOUND_TOPIC,
            envelope_model=_S6_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S6_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S6",
            topic=_INBOUND_TOPIC,
            envelope_model=_S6_DECLARED_MODEL,
            envelope_version=UNVERSIONED_MODEL,
            key_fields=_S6_KEY_FIELDS,
        )

        verdict = run_registry_match(
            edge_id="S6",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_model_class(
                edge_id="S6",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=_contract_declared_inbound_topic(),
                model_cls=_contract_declared_input_model(),
                field_names=tuple(name for name, _ in _S6_KEY_FIELDS),
            ),
            observed_consumer=observed_projection_from_instance(
                edge_id="S6",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=output.result.canonical_topic,
                instance=output.result,
                field_names=tuple(name for name, _ in _S6_KEY_FIELDS),
            ),
        )

        assert_registry_classification("S6", verdict)
        assert_regenerable("S6", verdict)

    def test_the_observed_producer_is_the_contract_not_this_module(self) -> None:
        """Prove the producer observation actually reads the wheel.

        Without this, "observed from the packaged contract" is a claim in a
        docstring. The resolved class must be the real
        ``ModelGatewayEnvelope`` the handlers transform, reached by importing
        the module path the contract names.
        """

        resolved = _contract_declared_input_model()

        assert resolved is ModelGatewayEnvelope
        assert model_identity(resolved) == _S6_DECLARED_MODEL
        assert _contract_declared_inbound_topic() == _INBOUND_TOPIC

    async def test_a_handler_that_stopped_stripping_reddens_the_consumer_leg(
        self, config: ModelGatewayForwarderConfig
    ) -> None:
        """Negative control on leg 3: observe the outbound handler instead.

        ``HandlerForwardOutbound`` legitimately produces the tenant-prefixed
        wire topic. Projecting its result against the inbound declaration is
        the shape a consume-side prefix-strip regression would take, and it
        must redden leg 3 rather than pass on envelope-model similarity.
        """

        output = await HandlerForwardOutbound(config).handle(
            _dispatched_dict(
                _gateway_envelope(
                    correlation_id=uuid4(),
                    canonical_topic=_OUTBOUND_TOPIC,
                    wire_topic=_OUTBOUND_WIRE,
                    source_topic=_OUTBOUND_TOPIC,
                    payload={"status": "completed"},
                )
            )
        )
        assert output.result is not None

        verdict = run_registry_match(
            edge_id="S6",
            declared_producer=producer_projection(
                edge_id="S6",
                topic=_INBOUND_TOPIC,
                envelope_model=_S6_DECLARED_MODEL,
                envelope_version=UNVERSIONED_MODEL,
                key_fields=_S6_KEY_FIELDS,
            ),
            declared_consumer=consumer_projection(
                edge_id="S6",
                topic=_INBOUND_TOPIC,
                envelope_model=_S6_DECLARED_MODEL,
                envelope_version=UNVERSIONED_MODEL,
                key_fields=_S6_KEY_FIELDS,
            ),
            observed_consumer=observed_projection_from_instance(
                edge_id="S6",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=output.result.wire_topic,
                instance=output.result,
                field_names=tuple(name for name, _ in _S6_KEY_FIELDS),
            ),
        )

        assert verdict.leg3_observed_consumer_vs_declared.passed is False
        assert verdict.leg3_observed_consumer_vs_declared.mismatching_field_path == (
            "topic"
        )
