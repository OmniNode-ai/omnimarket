"""Offline recorded-acceptance composition coverage.

The C0 request below is deliberately synthetic test provenance.  The B2
inputs are decoded from the immutable public B2 V2 fixture and its pinned B1
and V5 dependencies; none represents a live capture or route authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml
from omnibase_core.models.runtime.golden_chain.model_golden_chain_fixture import (
    ModelGoldenChainProvenance,
)

from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.handlers.handler_rsd_offline_c0 import (
    RsdOfflineC0ValidationError,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
)
from omnimarket.rsd import b1_projection_binding as binding
from omnimarket.rsd import container_bootstrap_artifact_evidence_v5 as evidence_v5
from omnimarket.rsd import (
    target_delivery_artifact_manifest_trust_anchor_v1 as anchor_v1,
)
from omnimarket.rsd import target_delivery_artifact_manifest_v2 as manifest_v2
from omnimarket.rsd import v4_static_profile as static_v4
from omnimarket.rsd.historical_target_delivery_map_v1 import TargetDeliveryMapV1
from omnimarket.rsd.recorded_acceptance import (
    RecordedRsdAcceptanceInput,
    RecordedRsdCrossBindingUnsupportedError,
    require_recorded_rsd_cross_binding,
    validate_recorded_rsd_acceptance,
)

_REPO = Path(__file__).parents[3]
_VECTORS = _REPO / "src/omnimarket/rsd/vectors"
_B2_V2_VECTOR = _VECTORS / "target_delivery_artifact_manifest_v2_public_vector.yaml"
_B2_V1_VECTOR = _VECTORS / "target_delivery_artifact_manifest_public_vector.yaml"
_V5_VECTOR = _VECTORS / "container_bootstrap_artifact_evidence_v5_public_vector.yaml"
_ROUTING_TIERS = (_REPO / "src/omnimarket/configs/routing_tiers.yaml").read_bytes()
_BIFROST = (_REPO / "src/omnimarket/configs/bifrost_delegation.yaml").read_bytes()
_REGISTRY = (
    _REPO / "src/omnimarket/data/model_registry/model_registry_v1.yaml"
).read_bytes()
_COMPONENTS = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)
_B2_V2_SHA256 = "5b91cfb95243403b3ddc234d154df355f46787f0c3ea49613d5bf404b1a72237"
_B2_V1_SHA256 = "4b10ec2b37f0768d0a8fa283d5a26cc6020e26a968ebb3928b78d4b8f73c65ed"
_V5_SHA256 = "6c66df411fd080f1d20e2cfe8f8004f600a1cfe1fe5942259b148af15166ca91"
_ENDPOINT = "https://recorded-acceptance-test.invalid/v1/chat/completions"


@dataclass(frozen=True)
class _B2Evidence:
    delivery_map: TargetDeliveryMapV1
    projection: static_v4.ContainerBootstrapStaticDeliveryProjectionV4
    b1_policy: binding.TargetDeliveryMapProjectionBindingTrustPolicyV1
    manifest_anchor: anchor_v1.TargetDeliveryArtifactManifestTrustAnchorV1
    role_inputs: tuple[
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
    ]
    policy_inputs: tuple[
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ]
    manifest: manifest_v2.TargetDeliveryArtifactManifestV2
    acceptance: manifest_v2.TargetDeliveryArtifactManifestAcceptanceV2


def _fixture_document(path: Path, expected_sha256: str) -> dict[str, object]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AssertionError("immutable public fixture hash changed")
    parsed = yaml.safe_load(raw)
    if type(parsed) is not dict:
        raise AssertionError("public fixture is not a mapping")
    return cast(dict[str, object], parsed)


def _carrier(value: object) -> bytes:
    if type(value) is not dict:
        raise AssertionError("fixture carrier is invalid")
    carrier = cast(dict[str, object], value)
    if set(carrier) != {"encoding", "segments"} or carrier["encoding"] != (
        "standard_base64_fixed_segments_v1"
    ):
        raise AssertionError("fixture carrier is invalid")
    segments = carrier["segments"]
    if type(segments) is not list or not all(type(item) is str for item in segments):
        raise AssertionError("fixture carrier is invalid")
    return base64.b64decode("".join(cast(list[str], segments)), validate=True)


def _tuples(value: object) -> object:
    """Restore JSON arrays to the tuple-only immutable evidence contract."""

    if type(value) is list:
        return tuple(_tuples(item) for item in cast(list[object], value))
    if type(value) is dict:
        return {
            key: _tuples(item) for key, item in cast(dict[str, object], value).items()
        }
    return value


def _canonical_json_model(model_type: type[Any], value: object) -> Any:
    payload = _carrier(value)
    decoded = json.loads(payload.decode("ascii"))
    model = model_type.model_validate(_tuples(decoded), strict=True)
    rendered = json.dumps(
        model.model_dump(mode="json", warnings="error"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if rendered != payload:
        raise AssertionError("public fixture model is not canonical")
    return model


@cache
def _b2_evidence() -> _B2Evidence:
    """Decode the existing immutable B2 public evidence, not a new fixture."""

    v2 = _fixture_document(_B2_V2_VECTOR, _B2_V2_SHA256)
    v1 = _fixture_document(_B2_V1_VECTOR, _B2_V1_SHA256)
    v5 = _fixture_document(_V5_VECTOR, _V5_SHA256)
    delivery_map = _canonical_json_model(TargetDeliveryMapV1, v1["target_delivery_map"])
    projection = static_v4.parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
        _carrier(v1["static_delivery_projection"])
    )
    b1_policy = binding.parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
        _carrier(v1["b1_policy"])
    )
    manifest_anchor = anchor_v1.parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(
        _carrier(v2["manifest_trust_anchor"])
    )
    manifest = manifest_v2.parse_target_delivery_artifact_manifest_v2_canonical_json(
        _carrier(v2["manifest"])
    )
    acceptance = manifest_v2.parse_target_delivery_artifact_manifest_acceptance_v2_canonical_json(
        _carrier(v2["expected_acceptance"])
    )
    v1_roles = cast(list[dict[str, object]], v1["roles"])
    v5_roles = cast(list[dict[str, object]], v5["roles"])
    role_inputs: list[manifest_v2.TargetDeliveryArtifactManifestRoleInputV2] = []
    policy_inputs: list[
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2
    ] = []
    for component, v1_role, v5_role in zip(
        _COMPONENTS, v1_roles, v5_roles, strict=True
    ):
        profile = static_v4.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            _carrier(v1_role["profile_envelope"])
        )
        projection_binding = (
            binding.parse_target_delivery_map_projection_binding_v1_canonical_json(
                _carrier(v1_role["projection_binding"])
            )
        )
        closure = evidence_v5.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            _carrier(v5_role["closure"])
        )
        policy = evidence_v5.parse_container_bootstrap_build_worker_trust_policy_v5_canonical_json(
            _carrier(v5_role["worker_policy"])
        )
        role_inputs.append(
            manifest_v2.TargetDeliveryArtifactManifestRoleInputV2(
                schema_version="rsd.target-delivery-artifact-manifest-role-input.v2",
                component=component,
                profile_envelope=profile,
                projection_binding=projection_binding,
                phase_a_v5_closure=closure,
            )
        )
        policy_inputs.append(
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2(
                schema_version=(
                    "rsd.target-delivery-artifact-manifest-v5-role-policy-input.v2"
                ),
                component=component,
                worker_trust_policy=policy,
            )
        )
    return _B2Evidence(
        delivery_map=delivery_map,
        projection=projection,
        b1_policy=b1_policy,
        manifest_anchor=manifest_anchor,
        role_inputs=cast(Any, tuple(role_inputs)),
        policy_inputs=cast(Any, tuple(policy_inputs)),
        manifest=manifest,
        acceptance=acceptance,
    )


def _synthetic_c0(*, endpoint: str = _ENDPOINT) -> ModelRsdOfflineC0Input:
    """Return synthetic test provenance, never a claimed recorded capture."""

    decision = ModelRoutingDecision(
        correlation_id=UUID("00000000-0000-0000-0000-000000000179"),
        task_type="code_generation",
        selected_model="Qwen3.6-35B-A3B",
        selected_backend_id=UUID("00000000-0000-0000-0000-000000000180"),
        endpoint_url=endpoint,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="synthetic offline test",
        rationale="synthetic offline test",
        tier_name="local",
        selected_backend_ref="local-coder",
    )
    overlay = (
        f"backends:\n  - backend_id: local-coder\n    endpoint_url: {endpoint}\n"
    ).encode("ascii")
    return ModelRsdOfflineC0Input(
        routing_decision=decision,
        golden_provenance=ModelGoldenChainProvenance(
            provider="local",
            model_id=decision.selected_model,
            endpoint_ref=decision.selected_backend_ref,
            endpoint=endpoint,
            request_hash="synthetic-request-not-a-capture",
            prompt_hash="synthetic-prompt-not-a-capture",
            routing_contract_hash="sha256:" + hashlib.sha256(_BIFROST).hexdigest(),
            routing_overlay_hash="sha256:" + hashlib.sha256(overlay).hexdigest(),
            recorded_at="2026-09-06T00:00:00Z",
            fixture_version="synthetic_recorded_acceptance_test.v1",
        ),
        routing_tiers_yaml=_ROUTING_TIERS,
        bifrost_contract_yaml=_BIFROST,
        bifrost_overlay_yaml=overlay,
        model_registry_yaml=_REGISTRY,
        model_registry_key="qwen3-coder-30b",
    )


def _recorded(**changes: object) -> RecordedRsdAcceptanceInput:
    evidence = _b2_evidence()
    baseline = RecordedRsdAcceptanceInput(
        c0=_synthetic_c0(),
        delivery_map=evidence.delivery_map,
        static_delivery_projection=evidence.projection,
        b1_trust_policy=evidence.b1_policy,
        manifest_trust_anchor=evidence.manifest_anchor,
        role_inputs=evidence.role_inputs,
        v5_role_policy_inputs=evidence.policy_inputs,
        manifest=evidence.manifest,
    )
    return replace(baseline, **changes)


@pytest.mark.unit
def test_recorded_acceptance_validates_synthetic_c0_and_immutable_b2_chain() -> None:
    result = validate_recorded_rsd_acceptance(_recorded())

    assert result.b2 == _b2_evidence().acceptance
    assert result.c0.backend_ref == "local-coder"
    assert result.c0.non_authorizing is True
    assert result.c0.effects_allowed is False
    assert result.cross_binding_status == "UNSUPPORTED"
    assert result.live_end_to_end_acceptance is False


@pytest.mark.unit
def test_valid_independent_c0_and_b2_never_claim_cross_binding() -> None:
    alternate_c0 = _synthetic_c0()
    alternate_c0 = alternate_c0.model_copy(
        update={
            "routing_decision": alternate_c0.routing_decision.model_copy(
                update={
                    "selected_backend_ref": "local-heavy-reasoning",
                    "max_context_tokens": 8192,
                }
            ),
            "bifrost_overlay_yaml": (
                b"backends:\n"
                b"  - backend_id: local-coder\n"
                + f"    endpoint_url: {_ENDPOINT}\n".encode("ascii")
                + b"  - backend_id: local-heavy-reasoning\n"
                + f"    endpoint_url: {_ENDPOINT}\n".encode("ascii")
            ),
        }
    )
    overlay = cast(bytes, alternate_c0.bifrost_overlay_yaml)
    alternate_c0 = alternate_c0.model_copy(
        update={
            "golden_provenance": alternate_c0.golden_provenance.model_copy(
                update={
                    "endpoint_ref": "local-heavy-reasoning",
                    "routing_overlay_hash": "sha256:"
                    + hashlib.sha256(overlay).hexdigest(),
                }
            )
        }
    )

    result = validate_recorded_rsd_acceptance(_recorded(c0=alternate_c0))

    assert result.c0.backend_ref == "local-heavy-reasoning"
    assert result.cross_binding_status == "UNSUPPORTED"
    assert result.live_end_to_end_acceptance is False
    with pytest.raises(RecordedRsdCrossBindingUnsupportedError):
        require_recorded_rsd_cross_binding(_recorded(c0=alternate_c0))


@pytest.mark.unit
def test_recorded_acceptance_rejects_synthetic_c0_route_endpoint_drift() -> None:
    c0 = _synthetic_c0()
    endpoint_drift = c0.model_copy(
        update={
            "routing_decision": c0.routing_decision.model_copy(
                update={"endpoint_url": "https://mismatch.invalid/v1"}
            )
        }
    )

    with pytest.raises(RsdOfflineC0ValidationError):
        validate_recorded_rsd_acceptance(_recorded(c0=endpoint_drift))


@pytest.mark.unit
def test_recorded_acceptance_rejects_b2_manifest_tamper() -> None:
    tampered = _b2_evidence().manifest.model_copy(update={"b1_policy_sha256": "0" * 64})

    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        validate_recorded_rsd_acceptance(_recorded(manifest=tampered))


@pytest.mark.unit
def test_recorded_acceptance_rejects_v5_policy_profile_substitution() -> None:
    policies = _b2_evidence().policy_inputs
    substituted = (policies[1], policies[0], policies[2], policies[3])

    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        validate_recorded_rsd_acceptance(_recorded(v5_role_policy_inputs=substituted))
