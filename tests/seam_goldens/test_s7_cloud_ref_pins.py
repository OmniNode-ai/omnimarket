# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S7 — the four cloud ``@ref`` pins that nothing resolves (severity=high).

The gateway contract declares four required reference pins under
``config.gateway_forwarder.cloud_leg`` — ``cloud_broker_ref``,
``cloud_auth_ref``, ``acl_provisioner_ref``, ``msk_region_ref`` — and the
config model requires them non-empty. But no resolver consumes them: the
deployed YAML hardcodes the literal broker list and region instead, so the
refs are declared, validated, carried, and never dereferenced. The registry
classifies this UNMATCHED at high severity, making it mandatory under the WS-7
union rule. It is also plausibly on-path, since the bridge must resolve broker
connectivity to reach the cloud bus at all.

A golden for an UNMATCHED edge proves the *absence* is real and structural.
The trap with absence proofs is that they can pass for the wrong reason — a
mistyped ref name would also "find no resolver". These goldens therefore pin
the producer side positively first (the exact four names exist in the real
packaged contract and flow into the real config model as opaque strings), then
prove the consumer side is missing, then prove the model treats the ref as a
literal rather than resolving it. Together those rule out the false-negative.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_cloud_bus_config import (
    ModelGatewayCloudBusConfig,
)

from tests.seam_goldens.harness import (
    assert_registry_classification,
    build_forwarder_config,
    gateway_cloud_leg,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

_REQUIRED_REFS: tuple[str, ...] = (
    "cloud_broker_ref",
    "cloud_auth_ref",
    "acl_provisioner_ref",
    "msk_region_ref",
)


class TestProducerSideDeclaresAllFourRefs:
    """Positive half — the pins genuinely exist, so absence below is real."""

    def test_slice_row_is_ws7_mandatory_high(self) -> None:
        edge = slice_edge("S7")
        assert edge.registry_severity == "high"
        assert edge.registry_classification == "UNMATCHED"

    @pytest.mark.parametrize("ref_name", _REQUIRED_REFS)
    def test_packaged_contract_declares_the_ref(self, ref_name: str) -> None:
        cloud_leg = gateway_cloud_leg()
        assert ref_name in cloud_leg
        assert str(cloud_leg[ref_name]).strip()

    @pytest.mark.parametrize("ref_name", _REQUIRED_REFS)
    def test_ref_reaches_the_real_config_model_unchanged(
        self, ref_name: str, tmp_path: Path
    ) -> None:
        """The ref survives config assembly as a literal string.

        This is the mechanical evidence of "declared but never dereferenced":
        the value that lands on the live config object is byte-identical to
        the contract's ref token. A resolver would have replaced it with a
        broker list or a region.
        """

        config = build_forwarder_config(dedupe_store_path=tmp_path / "dedupe.sqlite")
        declared = str(gateway_cloud_leg()[ref_name])

        assert getattr(config.cloud_bus, ref_name) == declared


class TestConsumerSideHasNoResolver:
    """Negative half — nothing turns a ref token into a real endpoint."""

    @pytest.mark.parametrize("ref_name", _REQUIRED_REFS)
    def test_ref_value_is_a_contract_token_not_a_broker_endpoint(
        self, ref_name: str
    ) -> None:
        """A resolved value would look like a host:port list or an AWS region.

        Asserting the *shape* is what makes this falsifiable: if a resolver is
        ever wired in, these values stop being dotted ref tokens and this test
        fails, forcing the registry to be re-derived rather than leaving S7
        recorded as UNMATCHED forever.
        """

        value = str(gateway_cloud_leg()[ref_name])

        assert value.startswith("gateway.cloud.kafka.")
        assert ":" not in value
        assert "," not in value
        assert not value.startswith("KAFKA_")

    def test_config_model_exposes_no_resolution_api(self) -> None:
        """The consumer that should dereference these does not exist."""

        resolver_shaped = [
            name
            for name in dir(ModelGatewayCloudBusConfig)
            if not name.startswith("__")
            and any(token in name.lower() for token in ("resolve", "dereference"))
        ]
        assert resolver_shaped == []

    def test_the_config_model_rejects_env_style_values_for_these_refs(self) -> None:
        """The refs are contract-scoped by validation, which is why the gap bites.

        The model actively refuses ``KAFKA_*`` env names for the ref fields, so
        an operator cannot close the gap by smuggling an env var through the
        ref. Deployment is pushed to hardcoding literals elsewhere — precisely
        the divergence the registry records.
        """

        cloud_leg = gateway_cloud_leg()
        with pytest.raises(ValueError, match="contract refs, not KAFKA_"):
            ModelGatewayCloudBusConfig(
                broker_provider_id=UUID(str(cloud_leg["broker_provider_id"])),
                cloud_broker_ref="KAFKA_BOOTSTRAP_SERVERS",
                cloud_auth_ref=str(cloud_leg["cloud_auth_ref"]),
                acl_provisioner_ref=str(cloud_leg["acl_provisioner_ref"]),
                msk_region_ref=str(cloud_leg["msk_region_ref"]),
                security_protocol="SASL_SSL",
                sasl_mechanism="AWS_MSK_IAM",
            )


class TestS7RegistryMatch:
    """The live match must reproduce UNMATCHED — a produced seam, no consumer."""

    def test_registry_match_reports_unmatched(self) -> None:
        verdict = run_registry_match(
            edge_id="S7",
            declared_producer=producer_projection(
                edge_id="S7",
                topic="config.gateway_forwarder.cloud_leg",
                envelope_model=(
                    "omnibase_infra.nodes.node_bus_forwarder_effect.models."
                    "model_gateway_cloud_bus_config.ModelGatewayCloudBusConfig"
                ),
                envelope_version="0.1.0",
                key_fields=tuple((name, "str") for name in _REQUIRED_REFS),
            ),
            # No declared consumer: there is no resolver on the other side.
            # Supplying a synthetic one would fabricate the very thing this
            # edge exists to record as missing.
            declared_consumer=None,
        )

        assert_registry_classification("S7", verdict)
        assert verdict.regenerability.value == "NOT_APPLICABLE"
        assert verdict.declared_consumer_hash is None

    def test_unmatched_edge_is_never_reported_regenerable(self) -> None:
        verdict = run_registry_match(
            edge_id="S7",
            declared_producer=producer_projection(
                edge_id="S7", topic="config.gateway_forwarder.cloud_leg"
            ),
            declared_consumer=None,
        )

        assert verdict.regenerability.value != "REGENERABLE"
