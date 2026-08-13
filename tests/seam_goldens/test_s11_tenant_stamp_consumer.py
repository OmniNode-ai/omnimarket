# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S11 — the verified-tenant payload stamp against its real consumer.

The gateway overwrites ``payload["tenant_id"]`` with the config-bound DNS-safe
slug immediately before the payload is dispatched to a local node; the consumer
is ``ModelDelegateSkillRequest``, which is ``extra="forbid"`` and documents
``tenant_id`` as a slug rather than a UUID.

This edge already has an ancestor test (``tests/integration/
test_tenant_stamp_seam_omn14208.py``) which reconstructs the producer's emitted
shape by hand, on the stated grounds that "the infra stamp symbols are not
importable here: they live in the infra repo, and infra->market is a forbidden
dependency direction". That rationale no longer holds — ``omnibase_infra`` is a
declared runtime dependency of this package, so
``omnibase_infra.shared.tenant_stamp.stamp_verified_tenant_slug`` imports
cleanly. These goldens therefore drive the REAL producer function rather than a
reconstruction of it, which is a strictly stronger proof: a reconstruction can
only ever confirm that the reconstruction matches the consumer.

Stronger still, the primary golden takes the stamp from a full gateway ingress
hop — real cloud bytes through the real ``ServiceGatewayForwarder`` — so what
reaches the consumer model is what the deployed path actually emits, not what
the helper emits when called directly.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_forwarder import (
    ServiceGatewayForwarder,
)
from omnibase_infra.shared.tenant_stamp import stamp_verified_tenant_slug
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
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

_INBOUND_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"
_INBOUND_WIRE = f"tenant-{GATEWAY_TENANT_SLUG}.{_INBOUND_TOPIC}"

_S11_KEY_FIELDS: tuple[tuple[str, str], ...] = (("tenant_id", "str"),)


def _raw_delegation_payload() -> dict[str, object]:
    return {
        "prompt": "summarize the changelog",
        "task_type": "summarization",
        "source": "claude-code",
    }


class TestRealProducerStampAgainstRealConsumer:
    """The canonical shape, driven end to end."""

    def test_slice_row_is_a_local_processing_edge(self) -> None:
        edge = slice_edge("S11")
        edge_is_local = "local" in edge.leg
        assert edge_is_local
        assert edge.traversed

    def test_real_stamp_helper_output_validates_against_the_consumer(self) -> None:
        stamped = stamp_verified_tenant_slug(
            _raw_delegation_payload(), GATEWAY_TENANT_SLUG
        )

        request = ModelDelegateSkillRequest.model_validate(stamped)

        assert request.tenant_id == GATEWAY_TENANT_SLUG

    def test_stamp_emits_a_slug_never_a_uuid(self) -> None:
        """The exact regression the canonical helper was written to close."""

        stamped = stamp_verified_tenant_slug(
            _raw_delegation_payload(), GATEWAY_TENANT_SLUG
        )

        assert stamped["tenant_id"] == GATEWAY_TENANT_SLUG
        assert stamped["tenant_id"] != str(GATEWAY_TENANT_ID)

    def test_stamp_emits_no_sibling_tenant_slug_key(self) -> None:
        """A sibling key would be rejected by the extra="forbid" consumer."""

        stamped = stamp_verified_tenant_slug(
            {**_raw_delegation_payload(), "tenant_slug": "attacker-supplied"},
            GATEWAY_TENANT_SLUG,
        )

        assert "tenant_slug" not in stamped
        assert ModelDelegateSkillRequest.model_validate(stamped).tenant_id == (
            GATEWAY_TENANT_SLUG
        )

    def test_client_supplied_tenant_id_is_overwritten_not_merged(self) -> None:
        """The config-bound slug is the trust anchor and must always win."""

        stamped = stamp_verified_tenant_slug(
            {**_raw_delegation_payload(), "tenant_id": "some-other-tenant"},
            GATEWAY_TENANT_SLUG,
        )

        assert stamped["tenant_id"] == GATEWAY_TENANT_SLUG

    def test_a_sibling_key_would_actually_be_rejected(self) -> None:
        """Negative control proving extra="forbid" is doing real work here.

        Without this, the "no sibling key" assertion above proves only that
        the helper does not emit one — not that emitting one would matter.
        """

        with pytest.raises(ValidationError):
            ModelDelegateSkillRequest.model_validate(
                {**_raw_delegation_payload(), "tenant_slug": GATEWAY_TENANT_SLUG}
            )


class TestStampFromTheRealGatewayIngressHop:
    """The stamp as the deployed path actually produces it."""

    async def test_ingress_payload_validates_against_the_consumer_model(
        self, tmp_path: Path
    ) -> None:
        local_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=local_bus,
            cloud_bus=RecordingPublisher(),
        )
        correlation_id = uuid4()

        await forwarder.consume_inbound_message(
            BusMessage(
                topic=_INBOUND_WIRE,
                value=cloud_hand_rolled_envelope_json(
                    envelope_id=uuid4(),
                    correlation_id=correlation_id,
                    event_type="omnibase-infra.delegation-request",
                    # The cloud publisher sends an UNSTAMPED payload; the slug
                    # is the gateway's contribution, which is the seam.
                    payload=_raw_delegation_payload(),
                    source_tenant_id=str(GATEWAY_TENANT_ID),
                    source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
                ),
            )
        )

        published = local_bus.only()
        envelope = published.envelope()

        request = ModelDelegateSkillRequest.model_validate(envelope.payload)

        assert request.tenant_id == GATEWAY_TENANT_SLUG
        assert_correlation_preserved(
            edge_id="S11",
            emitted=correlation_id,
            observed=envelope.correlation_id,
        )

    async def test_forged_payload_tenant_id_does_not_survive_the_hop(
        self, tmp_path: Path
    ) -> None:
        """A client-forged tenant claim must be replaced, not honoured."""

        local_bus = RecordingPublisher()
        forwarder = ServiceGatewayForwarder(
            config=build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite"),
            local_bus=local_bus,
            cloud_bus=RecordingPublisher(),
        )

        await forwarder.consume_inbound_message(
            BusMessage(
                topic=_INBOUND_WIRE,
                value=cloud_hand_rolled_envelope_json(
                    envelope_id=uuid4(),
                    correlation_id=uuid4(),
                    event_type="omnibase-infra.delegation-request",
                    payload={
                        **_raw_delegation_payload(),
                        "tenant_id": "victim-tenant",
                    },
                    source_tenant_id=str(GATEWAY_TENANT_ID),
                    source_tenant_principal_id=GATEWAY_PRINCIPAL_ID,
                ),
            )
        )

        request = ModelDelegateSkillRequest.model_validate(
            local_bus.only().envelope().payload
        )
        assert request.tenant_id == GATEWAY_TENANT_SLUG


class TestS11RegistryMatch:
    def test_registry_match_is_regenerable_for_the_driven_stamp(self) -> None:
        declared_producer = producer_projection(
            edge_id="S11",
            topic=_INBOUND_TOPIC,
            envelope_model=(
                "omnimarket.models.delegation.wire.model_delegate_skill_request."
                "ModelDelegateSkillRequest"
            ),
            envelope_version="1.0.0",
            key_fields=_S11_KEY_FIELDS,
        )
        declared_consumer = consumer_projection(
            edge_id="S11",
            topic=_INBOUND_TOPIC,
            envelope_model=(
                "omnimarket.models.delegation.wire.model_delegate_skill_request."
                "ModelDelegateSkillRequest"
            ),
            envelope_version="1.0.0",
            key_fields=_S11_KEY_FIELDS,
        )

        verdict = run_registry_match(
            edge_id="S11",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=declared_producer,
            observed_consumer=declared_consumer,
        )

        assert_registry_classification("S11", verdict)
        assert verdict.regenerability.value == "REGENERABLE"
