# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S6 — the contract -> handler seam at the ingress/bridge, scored at the pin.

The registry classifies S6 ``UNMATCHED`` / severity ``high``, re-scored
2026-08-14 under the seam graph's ``tracing_convention`` A (``PINNED_REF``,
OMN-16033): an edge is measured against the ``omnibase_infra`` revision this
repo actually resolves — ``b6bd79c34`` — not that repo's upstream HEAD. The
delegation commit that made this edge look matched (``642e60a87``) is not an
ancestor of the pin, and both trees report version ``0.38.4``, which is why the
staleness was invisible. This module is the executable half of that finding.

What the pin actually contains, and what these goldens drive:

* The PRODUCER side is real and observable. The ``contract.yaml`` packaged
  inside the pinned wheel declares both COMPUTE operations
  (``gateway.forward_outbound`` / ``gateway.consume_inbound``) and names
  ``ModelGatewayEnvelope`` as its IO model. The goldens read that file and
  resolve the named classes by import, so a contract that renames its IO model
  or drops a mirror topic fails here.
* Both handlers really work when driven — through their genuine dispatch
  entrypoint, ``handle()`` with the outer envelope delivered as a raw ``dict``
  (the OMN-13580 coercion path), not the convenience typed form. Correlation
  preservation is asserted in both directions because ``handle()`` contractually
  copies ``correlation_id`` from the input envelope.
* The CONSUMER side does not exist. Nothing in the executing path dispatches
  either declared operation: ``runtime/gateway_forwarder.py`` constructs
  ``ServiceGatewayForwarder`` directly, and that service re-derives the
  tenant-prefix transform locally without importing either handler class.
  ``test_pinned_infra_does_not_delegate_to_the_contract_handlers`` pins exactly
  that, and it is the positive evidence for the ``UNMATCHED`` row rather than a
  divergence from it.

So the edge is ``NOT_CLAIMED`` in ``slice_manifest.yaml``: an ``UNMATCHED``
edge has no second side, so this module supplies no observed projection at all
(``test_no_tautological_observations`` enforces that a NOT_CLAIMED-only module
never does). The registry-match leg is driven with ``declared_consumer=None``,
the faithful encoding of "there is no consumer", and must reproduce
``UNMATCHED``.

What the goldens still assert positively is the property whose violation is the
actual outage: the two independent implementations of the tenant-prefix
transform — the contract-declared handlers and the executing service — must
produce the same canonical/wire topic and stamp the same verified tenant. If
infra lands the delegation AND this repo moves the pin past ``642e60a87``, the
non-delegation pin fails first; that failure is the signal to re-score the row,
not to delete the assertion.
"""

from __future__ import annotations

import importlib
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
    UNVERSIONED_MODEL,
    BusMessage,
    RecordingPublisher,
    assert_correlation_preserved,
    assert_registry_classification,
    build_forwarder_config,
    cloud_hand_rolled_envelope_json,
    gateway_contract_version,
    gateway_mirror_topics,
    load_gateway_contract,
    local_typed_envelope,
    model_identity,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import (
    EnumSeamObservationClass,
    EnumSliceInclusion,
    slice_edge,
)

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
_EXPECTED_CONTRACT_VERSION = "0.1.0"


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

    def test_slice_row_records_the_pinned_scoring(self) -> None:
        """The frozen row must say what the pin measurement found.

        Read from the manifest rather than restated here, so the golden and the
        frozen slice cannot drift: the edge covers both declared operations, is
        mandatory because the registry rates it ``high``, and does NOT claim the
        Milestone-B receipt traverses it — at the pin nothing dispatches these
        operations, so the receipt crosses the forwarder component without ever
        crossing this seam.
        """

        edge = slice_edge("S6")
        assert "both declared operations" in edge.leg
        assert edge.inclusion is EnumSliceInclusion.WS7_MANDATORY_HIGH
        assert edge.registry_severity == "high"
        assert not edge.traversed

    def test_slice_row_records_the_consumer_side_as_unobservable(self) -> None:
        """The entitlement this module is allowed to claim, bound to the manifest.

        ``UNMATCHED`` means one side was never found. The manifest records that
        as ``consumer_symbol_reachable: false`` / ``NOT_CLAIMED``; asserting it
        through the loader (not as a literal in this file) is what stops a
        future edit from quietly re-upgrading the claim while the seam is still
        one-sided.
        """

        edge = slice_edge("S6")
        assert edge.producer_symbol_reachable
        assert not edge.consumer_symbol_reachable
        assert edge.observation_class is EnumSeamObservationClass.NOT_CLAIMED

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
    """The evidence for ``UNMATCHED``, plus the parity property that must hold.

    See the module docstring: at the pinned ``omnibase_infra`` rev the
    contract-declared handlers have no live caller, so this is where that fact
    is measured rather than asserted in prose — and where the risk it creates
    (two implementations of one transform, drifting silently) is pinned.
    """

    def test_pinned_infra_does_not_delegate_to_the_contract_handlers(
        self,
    ) -> None:
        """Measure the missing consumer side that makes S6 ``UNMATCHED``.

        The registry row states that the executing surface and the
        contract-declared surface are two separate implementations, and that
        nothing invokes the declared operations. The executable form of that
        claim is this: the module that actually runs in the gateway process
        never references either handler class, so editing
        ``HandlerForwardOutbound`` / ``HandlerConsumeInbound`` cannot change
        what the pinned wheel executes.

        This is a measurement, not an endorsement. It fails the moment infra's
        delegation (``642e60a87``) enters this repo's resolved dependency
        closure — i.e. when the pin moves past it — and that failure is the
        intended signal: under ``tracing_convention`` A, moving a pin is a seam
        event, so re-score the row, re-derive ``seams.v1.yaml``, and only then
        replace this assertion.
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
    def test_packaged_contract_version_is_pinned(self) -> None:
        """A wheel bump that moves the contract version is a decision.

        Kept as its own assertion rather than folded into the projection: the
        gateway envelope carries no wire version, so putting a semver on the
        projection would have been decoration. Here it is a real check against
        the packaged file.
        """

        assert gateway_contract_version() == _EXPECTED_CONTRACT_VERSION

    def test_registry_match_reports_unmatched(self) -> None:
        """The live match must reproduce ``UNMATCHED`` — a declared seam, no consumer.

        ``declared_consumer=None`` is the faithful encoding, and the same shape
        S3/S7 use: at the pinned rev nothing invokes ``gateway.consume_inbound``
        or ``gateway.forward_outbound``, so there is no consuming side to
        project. Synthesising one would manufacture the agreement this row
        exists to record as absent — and would put this module back in the
        business of supplying an observation for an edge that has no second
        side.

        The declared PRODUCER is not synthesised either: its topic is the
        inbound mirror topic read out of the contract packaged in the pinned
        wheel.
        """

        verdict = run_registry_match(
            edge_id="S6",
            declared_producer=producer_projection(
                edge_id="S6",
                topic=_contract_declared_inbound_topic(),
                envelope_model=_S6_DECLARED_MODEL,
                envelope_version=UNVERSIONED_MODEL,
                key_fields=_S6_KEY_FIELDS,
            ),
            declared_consumer=None,
        )

        assert_registry_classification("S6", verdict)
        assert verdict.regenerability.value == "NOT_APPLICABLE"
        assert verdict.declared_consumer_hash is None

    def test_unmatched_edge_is_never_reported_regenerable(self) -> None:
        """A one-sided edge can never earn the strongest claim.

        The manifest already records ``NOT_CLAIMED``; this asserts the shipped
        classifier agrees, so the entitlement and the runtime verdict cannot
        drift apart in the direction that would let the row look healthier than
        the seam is.
        """

        verdict = run_registry_match(
            edge_id="S6",
            declared_producer=producer_projection(
                edge_id="S6", topic=_contract_declared_inbound_topic()
            ),
            declared_consumer=None,
        )

        assert verdict.regenerability.value != "REGENERABLE"

    def test_the_declared_producer_is_the_contract_not_this_module(self) -> None:
        """Prove the producer side actually reads the wheel.

        Without this, "declared by the packaged contract" is a claim in a
        docstring. The resolved class must be the real ``ModelGatewayEnvelope``
        the handlers transform, reached by importing the module path the
        contract names, on the mirror topic that same file declares.
        """

        resolved = _contract_declared_input_model()

        assert resolved is ModelGatewayEnvelope
        assert model_identity(resolved) == _S6_DECLARED_MODEL
        assert _contract_declared_inbound_topic() == _INBOUND_TOPIC
