# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerSeamMatch (node_seam_match_compute, OMN-15763).

Covers: canonical seam-projection/v1 serialization (schema_version field,
sorted-key/model_dump_json round-trip idiom, determinism), the three-leg
structural diff naming the exact mismatching field path, sha256 per-edge
hash pinning, and the stale-proof detector (hash flip without a golden
re-pin surfaces as "seam changed, proof stale").
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_seam_match_compute.handlers.handler_seam_match import (
    HandlerSeamMatch,
    check_stale_proof,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_request import (
    ModelSeamMatchRequest,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_verdict import (
    EnumSeamMatchVerdict,
    EnumSeamRegenerabilityClass,
)
from omnimarket.seams.canonical import canonical_json, canonical_sha256
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    EnumSeamProjectionRole,
    ModelSeamProjection,
    ModelSeamProjectionField,
)


def _projection(
    *,
    edge_id: str = "S1",
    role: EnumSeamProjectionRole = EnumSeamProjectionRole.PRODUCER,
    topic: str = "tenant-{slug}.onex.cmd.omnibase-infra.delegation-request.v1",
    envelope_model: str = "omnibase_core.models.wire.model_delegation_routing_input.ModelDelegationRoutingInput",
    envelope_version: str = "1.0.0",
    key_fields: tuple[ModelSeamProjectionField, ...] = (
        ModelSeamProjectionField(name="tenant_id", field_type="str"),
        ModelSeamProjectionField(name="request_id", field_type="uuid.UUID"),
    ),
    delivery_semantics: EnumSeamDeliverySemantics = EnumSeamDeliverySemantics.AT_LEAST_ONCE,
) -> ModelSeamProjection:
    return ModelSeamProjection(
        edge_id=edge_id,
        role=role,
        topic=topic,
        envelope_model=envelope_model,
        envelope_version=envelope_version,
        key_fields=key_fields,
        delivery_semantics=delivery_semantics,
    )


@pytest.mark.unit
class TestCanonicalSerialization:
    def test_schema_version_field_present_and_pinned(self) -> None:
        projection = _projection()
        assert projection.schema_version == "seam-projection/v1"

    def test_round_trip_idiom_matches_model_dump_json(self) -> None:
        projection = _projection()
        # tests/golden_chains/regression_replay round-trip idiom
        assert json.loads(projection.model_dump_json()) == projection.model_dump(
            mode="json"
        )

    def test_canonical_json_is_sorted_key_and_deterministic(self) -> None:
        projection = _projection()
        first = canonical_json(projection)
        second = canonical_json(projection)
        assert first == second
        parsed = json.loads(first)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_canonical_json_independent_of_construction_kwarg_order(self) -> None:
        a = ModelSeamProjection(
            edge_id="S1",
            role=EnumSeamProjectionRole.PRODUCER,
            topic="t",
            envelope_model="m",
            envelope_version="1.0.0",
        )
        b = ModelSeamProjection(
            envelope_version="1.0.0",
            envelope_model="m",
            topic="t",
            role=EnumSeamProjectionRole.PRODUCER,
            edge_id="S1",
        )
        assert canonical_json(a) == canonical_json(b)

    def test_field_change_changes_canonical_json(self) -> None:
        a = _projection(topic="topic-a")
        b = _projection(topic="topic-b")
        assert canonical_json(a) != canonical_json(b)


@pytest.mark.unit
class TestHashPinning:
    def test_sha256_is_deterministic_across_repeat_calls(self) -> None:
        projection = _projection()
        assert canonical_sha256(projection) == canonical_sha256(projection)

    def test_sha256_changes_when_wire_field_changes(self) -> None:
        a = _projection(envelope_version="1.0.0")
        b = _projection(envelope_version="2.0.0")
        assert canonical_sha256(a) != canonical_sha256(b)

    def test_sha256_is_64_char_hex(self) -> None:
        digest = canonical_sha256(_projection())
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not hex


@pytest.mark.unit
class TestThreeLegMatch:
    def test_matched_declared_only_is_shape_only_not_regenerable(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=producer, declared_consumer=consumer
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.MATCHED
        assert verdict.regenerability == EnumSeamRegenerabilityClass.SHAPE_ONLY
        assert verdict.leg1_declared_vs_declared.passed is True
        assert verdict.leg2_observed_producer_vs_declared.passed is None
        assert verdict.leg3_observed_consumer_vs_declared.passed is None

    def test_all_three_legs_green_is_regenerable(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        request = ModelSeamMatchRequest(
            edge_id="S1",
            declared_producer=producer,
            declared_consumer=consumer,
            observed_producer=producer,
            observed_consumer=consumer,
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.MATCHED
        assert verdict.regenerability == EnumSeamRegenerabilityClass.REGENERABLE
        assert verdict.leg1_declared_vs_declared.passed is True
        assert verdict.leg2_observed_producer_vs_declared.passed is True
        assert verdict.leg3_observed_consumer_vs_declared.passed is True

    def test_observed_producer_drift_fails_leg2_and_blocks_regenerable(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        drifted_producer = _projection(
            role=EnumSeamProjectionRole.PRODUCER, topic="bare.topic.no.prefix"
        )
        request = ModelSeamMatchRequest(
            edge_id="S1",
            declared_producer=producer,
            declared_consumer=consumer,
            observed_producer=drifted_producer,
            observed_consumer=consumer,
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.regenerability != EnumSeamRegenerabilityClass.REGENERABLE
        assert verdict.leg2_observed_producer_vs_declared.passed is False
        assert (
            verdict.leg2_observed_producer_vs_declared.mismatching_field_path == "topic"
        )

    def test_missing_consumer_is_unmatched(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=producer, declared_consumer=None
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.UNMATCHED
        assert verdict.regenerability == EnumSeamRegenerabilityClass.NOT_APPLICABLE

    def test_missing_producer_is_unmatched(self) -> None:
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=None, declared_consumer=consumer
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.UNMATCHED

    def test_topic_mismatch_names_exact_field_path_not_boolean(self) -> None:
        producer = _projection(
            role=EnumSeamProjectionRole.PRODUCER, topic="bare.onex.cmd.v1"
        )
        consumer = _projection(
            role=EnumSeamProjectionRole.CONSUMER,
            topic="tenant-x.onex.cmd.v1",
        )
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=producer, declared_consumer=consumer
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.MISMATCH
        assert verdict.leg1_declared_vs_declared.passed is False
        assert verdict.leg1_declared_vs_declared.mismatching_field_path == "topic"
        assert verdict.regenerability == EnumSeamRegenerabilityClass.NOT_APPLICABLE

    def test_key_field_type_mismatch_names_nested_field_path(self) -> None:
        producer = _projection(
            role=EnumSeamProjectionRole.PRODUCER,
            key_fields=(
                ModelSeamProjectionField(name="request_id", field_type="uuid.UUID"),
            ),
        )
        consumer = _projection(
            role=EnumSeamProjectionRole.CONSUMER,
            key_fields=(ModelSeamProjectionField(name="request_id", field_type="str"),),
        )
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=producer, declared_consumer=consumer
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.MISMATCH
        mismatching_field_path = (
            verdict.leg1_declared_vs_declared.mismatching_field_path
        )
        assert mismatching_field_path is not None
        assert "key_fields" in mismatching_field_path


@pytest.mark.unit
class TestStaleProofDetector:
    def test_matching_pin_is_not_stale(self) -> None:
        producer = _projection()
        pinned = canonical_sha256(producer)
        result = check_stale_proof(
            edge_id="S1", pinned_hash=pinned, current_producer=producer
        )
        assert result.stale is False
        assert result.pinned_hash == result.current_hash

    def test_hash_flip_without_repin_is_stale(self) -> None:
        producer = _projection(envelope_version="1.0.0")
        pinned = canonical_sha256(producer)
        drifted = _projection(envelope_version="2.0.0")
        result = check_stale_proof(
            edge_id="S1", pinned_hash=pinned, current_producer=drifted
        )
        assert result.stale is True
        assert result.detail == "seam changed, proof stale"
        assert result.pinned_hash != result.current_hash


@pytest.mark.unit
class TestStaleProofWiredIntoHandle:
    """CodeRabbit finding: ModelSeamMatchRequest.pinned_hash was declared but
    handle() never read it, so the node entry point could never report a
    stale proof. These verify the wiring, not just the standalone helper."""

    def test_matching_pinned_hash_yields_non_stale_verdict(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        pinned = canonical_sha256(producer)
        request = ModelSeamMatchRequest(
            edge_id="S1",
            declared_producer=producer,
            declared_consumer=consumer,
            pinned_hash=pinned,
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.stale_proof is not None
        assert verdict.stale_proof.stale is False

    def test_drifted_pinned_hash_yields_stale_verdict(self) -> None:
        producer = _projection(
            role=EnumSeamProjectionRole.PRODUCER, envelope_version="1.0.0"
        )
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        stale_pin = canonical_sha256(_projection(envelope_version="0.1.0"))
        request = ModelSeamMatchRequest(
            edge_id="S1",
            declared_producer=producer,
            declared_consumer=consumer,
            pinned_hash=stale_pin,
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.stale_proof is not None
        assert verdict.stale_proof.stale is True
        assert verdict.stale_proof.detail == "seam changed, proof stale"

    def test_no_pinned_hash_yields_none_not_false_not_stale(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        consumer = _projection(role=EnumSeamProjectionRole.CONSUMER)
        request = ModelSeamMatchRequest(
            edge_id="S1", declared_producer=producer, declared_consumer=consumer
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.stale_proof is None

    def test_stale_proof_still_computed_on_unmatched_edge(self) -> None:
        producer = _projection(role=EnumSeamProjectionRole.PRODUCER)
        pinned = canonical_sha256(producer)
        request = ModelSeamMatchRequest(
            edge_id="S1",
            declared_producer=producer,
            declared_consumer=None,
            pinned_hash=pinned,
        )
        verdict = HandlerSeamMatch().handle(request)
        assert verdict.verdict == EnumSeamMatchVerdict.UNMATCHED
        assert verdict.stale_proof is not None
        assert verdict.stale_proof.stale is False
