# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S4 + S5 — the ingress/bridge wire body and its tenant-attribution gate.

Both edges sit on the same forward leg as S1 and are goldened against the real
``ServiceGatewayForwarder`` from the pinned ``omnibase_infra`` wheel — the
actual bridge component, running its actual decode / validate / transform /
publish path with nothing patched inside the seam.

S4 (severity=high, WS-7 mandatory) is the JSON envelope body itself: the cloud
MSK publisher hand-rolls a dict literal and the local side parses it with
``ModelEventEnvelope[dict[str, object]].model_validate_json``. Two
independently written implementations of one wire format is precisely the
shape of defect that a per-side unit test cannot see, so the golden builds the
producer body as a raw literal (never by serializing the consumer's own model)
and feeds those exact bytes to the real consumer.

S5 is the tenant-attribution gate on that same body: the forwarder equality-
checks ``metadata.tags.source_tenant_id`` / ``source_tenant_principal_id``
against its own config-bound identity and raises ``ValueError`` otherwise. A
golden that only proved the happy path would leave the security-relevant half
of the seam unproven, so both the accept and the reject direction are driven.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

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
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_CANONICAL_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_WIRE_TOPIC = f"tenant-{GATEWAY_TENANT_SLUG}.{_CANONICAL_TOPIC}"

# The exact producer field set the registry records for S4.
_S4_PRODUCER_FIELDS: tuple[str, ...] = (
    "envelope_id",
    "correlation_id",
    "source_tool",
    "metadata",
    "event_type",
    "priority",
    "retry_count",
    "onex_version",
    "envelope_version",
)

_S4_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("envelope_id", "UUID"),
    ("correlation_id", "UUID"),
    ("source_tool", "str"),
    ("event_type", "str"),
    ("priority", "int"),
    ("retry_count", "int"),
    ("onex_version", "ModelSemVer"),
    ("envelope_version", "ModelSemVer"),
)

_S5_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("source_tenant_id", "str"),
    ("source_tenant_principal_id", "str"),
)


@pytest.fixture
def forwarder(tmp_path: Path) -> ServiceGatewayForwarder:
    """The real bridge service, wired to recording transports on both legs."""

    return ServiceGatewayForwarder(
        config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
        local_bus=RecordingPublisher(),
        cloud_bus=RecordingPublisher(),
    )


def _cloud_message(
    *,
    correlation_id: UUID,
    envelope_id: UUID | None = None,
    source_tenant_id: str | None = None,
    source_tenant_principal_id: str | None = None,
) -> BusMessage:
    return BusMessage(
        topic=_WIRE_TOPIC,
        value=cloud_hand_rolled_envelope_json(
            envelope_id=envelope_id or uuid4(),
            correlation_id=correlation_id,
            event_type="omnibase-infra.delegation-request",
            payload={
                "prompt": "summarize the changelog",
                "task_type": "summarization",
                "source": "claude-code",
            },
            source_tenant_id=source_tenant_id or str(GATEWAY_TENANT_ID),
            source_tenant_principal_id=(
                source_tenant_principal_id or GATEWAY_PRINCIPAL_ID
            ),
        ),
    )


class TestS4WireBody:
    """The hand-rolled cloud JSON body against the typed local parse."""

    def test_slice_row_is_ws7_mandatory_high(self) -> None:
        edge = slice_edge("S4")
        assert edge.registry_severity == "high"
        assert edge.traversed

    def test_producer_body_carries_exactly_the_registry_field_set(self) -> None:
        """Pin the producer shape so a silent field drop is caught here."""

        body = json.loads(
            cloud_hand_rolled_envelope_json(
                envelope_id=uuid4(),
                correlation_id=uuid4(),
                event_type="omnibase-infra.delegation-request",
                payload={},
                source_tenant_id=str(GATEWAY_TENANT_ID),
                source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
            )
        )
        for name in _S4_PRODUCER_FIELDS:
            assert name in body, f"S4 producer body lost field {name}"
        assert set(body["metadata"]) == {"headers", "tags"}

    def test_real_bridge_decodes_the_hand_rolled_body(self) -> None:
        """Drive the bridge's own ``decode_message`` over the producer bytes."""

        correlation_id = uuid4()
        envelope_id = uuid4()
        message = _cloud_message(correlation_id=correlation_id, envelope_id=envelope_id)

        decoded = ServiceGatewayForwarder.decode_message(message)

        assert_correlation_preserved(
            edge_id="S4", emitted=correlation_id, observed=decoded.correlation_id
        )
        assert decoded.envelope_id == envelope_id
        assert decoded.source_tool == "onex-api"
        assert decoded.event_type == "omnibase-infra.delegation-request"
        assert decoded.priority == 5
        assert decoded.retry_count == 0
        assert decoded.envelope_version.major == 2
        assert decoded.envelope_version.minor == 1

    def test_registry_match_is_regenerable_for_the_driven_body(self) -> None:
        declared_producer = producer_projection(
            edge_id="S4", topic=_WIRE_TOPIC, key_fields=_S4_KEY_FIELDS
        )
        declared_consumer = consumer_projection(
            edge_id="S4", topic=_WIRE_TOPIC, key_fields=_S4_KEY_FIELDS
        )

        verdict = run_registry_match(
            edge_id="S4",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S4", verdict)
        assert verdict.regenerability.value == "REGENERABLE"

    def test_a_body_missing_correlation_id_is_caught_not_defaulted(self) -> None:
        """Negative control on the property that matters most for this slice.

        ``ModelEventEnvelope.correlation_id`` defaults to ``None``, so a
        producer that stops emitting it still parses cleanly — the seam looks
        healthy while every downstream correlation join silently breaks. The
        correlation assertion must reject that, and this proves it does.
        """

        body = json.loads(
            cloud_hand_rolled_envelope_json(
                envelope_id=uuid4(),
                correlation_id=uuid4(),
                event_type="omnibase-infra.delegation-request",
                payload={},
                source_tenant_id=str(GATEWAY_TENANT_ID),
                source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
            )
        )
        del body["correlation_id"]

        decoded = ServiceGatewayForwarder.decode_message(
            BusMessage(topic=_WIRE_TOPIC, value=json.dumps(body).encode("utf-8"))
        )
        assert decoded.correlation_id is None

        with pytest.raises(AssertionError, match="correlation_id was dropped"):
            assert_correlation_preserved(
                edge_id="S4", emitted=uuid4(), observed=decoded.correlation_id
            )


class TestS5TenantAttributionTags:
    """The forwarder's identity equality check on the same forward leg."""

    def test_slice_row_is_on_the_forward_leg(self) -> None:
        edge = slice_edge("S5")
        assert edge.traversed
        assert "cloud->local" in edge.leg

    def test_matching_tags_are_accepted_by_the_real_gate(
        self, forwarder: ServiceGatewayForwarder
    ) -> None:
        message = _cloud_message(correlation_id=uuid4())

        # Raises if the gate rejects; no assertion needed beyond not raising,
        # which is exactly the production contract of this method.
        forwarder.validate_inbound_message(message)

    def test_mismatched_tenant_id_is_rejected(
        self, forwarder: ServiceGatewayForwarder
    ) -> None:
        message = _cloud_message(
            correlation_id=uuid4(),
            source_tenant_id=str(uuid4()),
        )

        with pytest.raises(ValueError, match="tenant_id does not match"):
            forwarder.validate_inbound_message(message)

    def test_mismatched_principal_id_is_rejected(
        self, forwarder: ServiceGatewayForwarder
    ) -> None:
        message = _cloud_message(
            correlation_id=uuid4(),
            source_tenant_principal_id=f"t-{uuid4().hex}",
        )

        with pytest.raises(ValueError, match="principal_id does not match"):
            forwarder.validate_inbound_message(message)

    def test_absent_tags_are_rejected_rather_than_treated_as_trusted(
        self, forwarder: ServiceGatewayForwarder
    ) -> None:
        """Fail-closed check: no tags must not read as "no objection"."""

        body = json.loads(_cloud_message(correlation_id=uuid4()).value)
        body["metadata"]["tags"] = {}

        with pytest.raises(ValueError, match="does not match attached tenant"):
            forwarder.validate_inbound_message(
                BusMessage(topic=_WIRE_TOPIC, value=json.dumps(body).encode("utf-8"))
            )

    async def test_accepted_message_reaches_the_local_bus_with_correlation(
        self, tmp_path: Path
    ) -> None:
        """Full ingress hop: cloud bytes in, local publish out, id intact."""

        local_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=local_bus,
            cloud_bus=RecordingPublisher(),
        )
        correlation_id = uuid4()

        await forwarder.consume_inbound_message(
            _cloud_message(correlation_id=correlation_id)
        )

        published = local_bus.only()
        assert published.topic == _CANONICAL_TOPIC
        assert_correlation_preserved(
            edge_id="S5",
            emitted=correlation_id,
            observed=published.envelope().correlation_id,
        )

    def test_registry_match_is_regenerable_for_the_driven_gate(self) -> None:
        declared_producer = producer_projection(
            edge_id="S5", topic=_WIRE_TOPIC, key_fields=_S5_KEY_FIELDS
        )
        declared_consumer = consumer_projection(
            edge_id="S5", topic=_WIRE_TOPIC, key_fields=_S5_KEY_FIELDS
        )

        verdict = run_registry_match(
            edge_id="S5",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S5", verdict)
        assert verdict.regenerability.value == "REGENERABLE"
