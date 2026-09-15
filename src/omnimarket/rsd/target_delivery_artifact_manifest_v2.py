"""Pure Phase-B2 V2 signed target-delivery artifact manifest.

V2 aggregates the original, signed V4 profile envelopes, B1 map-to-profile
relations, and V5 worker closures for the four fixed delivery roles.  It is a
pure offline verifier: an acceptance is diagnostic only and cannot replace
the original evidence on a later authorization path.

The B1 policy is used only to revalidate the map -> projection -> profile
relation.  Each V5 closure instead receives its own caller-pinned V5 policy;
the signed manifest records the exact V5 policy hash selected for each role.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Literal, NoReturn, Self, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from omnimarket.rsd.b1_projection_binding import (
    TargetDeliveryMapProjectionBindingTrustPolicyV1,
    TargetDeliveryMapProjectionBindingV1,
    parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json,
    target_delivery_map_projection_binding_trust_policy_v1_canonical_json,
    validate_target_delivery_map_projection_binding_v1,
)
from omnimarket.rsd.container_bootstrap_artifact_evidence_v5 import (
    ContainerBootstrapArtifactEvidenceClosureV5,
    ContainerBootstrapArtifactWorkerAttestationV5,
    ContainerBootstrapBuildWorkerTrustPolicyV5,
    container_bootstrap_artifact_evidence_closure_v5_canonical_json,
    container_bootstrap_artifact_worker_attestation_v5_sha256,
    container_bootstrap_build_worker_trust_policy_v5_sha256,
    validate_container_bootstrap_artifact_evidence_closure_v5,
)
from omnimarket.rsd.container_bootstrap_oci_safe_config_evidence_v5 import (
    ContainerBootstrapOciSafeConfigEvidenceV5,
    container_bootstrap_oci_safe_config_evidence_v5_sha256,
)
from omnimarket.rsd.historical_target_delivery_map_v1 import TargetDeliveryMapV1
from omnimarket.rsd.oci_config_commitment import (
    PhaseAV5ExpandedOciConfigCommitmentClaimV1,
)
from omnimarket.rsd.oci_repository import (
    oci_repository_reference_v1,
    validate_oci_repository_reference_v1,
    validate_oci_repository_v1,
)
from omnimarket.rsd.target_delivery_artifact_manifest_trust_anchor_v1 import (
    TargetDeliveryArtifactManifestTrustAnchorV1,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticDeliveryProjectionV4,
    ContainerBootstrapStaticRoleProfileEnvelopeV4,
    container_bootstrap_static_delivery_projection_v4_canonical_json,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    parse_container_bootstrap_static_delivery_projection_v4_canonical_json,
)

_SHA256 = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_PATH = r"^/[A-Za-z0-9._/-]{1,240}$"
_RELATIVE_PATH = r"^[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$"
_OCI_CONFIG_DESCRIPTOR_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_COMPONENTS = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)
_ROLES = {
    "primary_infisical": "infisical",
    "primary_valkey": "valkey",
    "restore_infisical": "infisical",
    "restore_valkey": "valkey",
}
_MAX_MANIFEST_BYTES = 65_536
_MAX_ACCEPTANCE_BYTES = 16_384
_MAX_ROLE_INPUT_BYTES = 1_048_576
_MAX_ROLE_POLICY_BYTES = 16_384
_MAX_DEPTH = 32
_MAX_NODES = 16_384
_MISSING_MODEL_STATE = object()
_MANIFEST_DOMAIN = b"omninode-rsd.target-delivery-artifact-manifest.ed25519.v2\x00"
_MANIFEST_HASH_DOMAIN = b"omninode-rsd.target-delivery-artifact-manifest.sha256.v2\x00"
_CONTEXT_DOMAIN = (
    b"omninode-rsd.target-delivery-artifact-manifest-context.sha256.v2\x00"
)
_B1_POLICY_HASH_DOMAIN = (
    b"omninode-rsd.target-delivery-artifact-manifest-b1-policy.sha256.v2\x00"
)
_SOURCE_TREE_ENTRIES_DOMAIN = (
    b"omninode-rsd.target-delivery-artifact-manifest-source-tree-entries.sha256.v2\x00"
)


class TargetDeliveryArtifactManifestV2Error(ValueError):
    """Fixed, value-redacted V2 validation failure."""

    __slots__ = ("phase",)

    def __init__(self, phase: Literal["parse", "anchor", "input", "manifest"]):
        super().__init__("target delivery artifact manifest V2 validation failed")
        self.phase = phase


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail(phase: Literal["parse", "anchor", "input", "manifest"]) -> NoReturn:
    raise TargetDeliveryArtifactManifestV2Error(phase)


def _b64(value: str) -> bytes:
    if type(value) is not str:
        raise ValueError("base64 is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("base64 is invalid") from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 is invalid")
    return decoded


def _exact_model_state(
    value: object,
    expected: type[BaseModel],
    *,
    active_path: set[int] | None = None,
) -> BaseModel:
    """Reject hidden/deleted/model_construct state before a dump can omit it."""

    active = set() if active_path is None else active_path
    try:
        if type(value) is not expected:
            raise ValueError("model type is invalid")
        model = value
        model_id = id(model)
        if model_id in active:
            raise ValueError("model state is invalid")
        active.add(model_id)
        try:
            fields = set(expected.model_fields)
            state = getattr(model, "__dict__", _MISSING_MODEL_STATE)
            extra = getattr(model, "__pydantic_extra__", _MISSING_MODEL_STATE)
            hidden_state = getattr(
                model, "__pydantic_" + "pri" + "vate__", _MISSING_MODEL_STATE
            )
            fields_set = getattr(model, "__pydantic_fields_set__", _MISSING_MODEL_STATE)
            if (
                type(state) is not dict
                or set(cast(dict[str, object], state)) != fields
                or extra is not None
                or hidden_state is not None
                or type(fields_set) is not set
                or fields_set != fields
            ):
                raise ValueError("model state is invalid")
            for field in expected.model_fields:
                _exact_value(cast(dict[str, object], state)[field], active_path=active)
        finally:
            active.remove(model_id)
        return model
    except RecursionError:
        raise ValueError("model state is invalid") from None


def _exact_value(value: object, *, active_path: set[int]) -> None:
    """Walk exact model state while rejecting only active-path object cycles."""

    try:
        if isinstance(value, BaseModel):
            _exact_model_state(value, type(value), active_path=active_path)
            return
        if type(value) not in (tuple, list, dict, set, frozenset):
            return
        value_id = id(value)
        if value_id in active_path:
            raise ValueError("model state is invalid")
        active_path.add(value_id)
        try:
            if type(value) is dict:
                for key, item in cast(dict[object, object], value).items():
                    _exact_value(key, active_path=active_path)
                    _exact_value(item, active_path=active_path)
            else:
                for item in cast(tuple[object, ...], value):
                    _exact_value(item, active_path=active_path)
        finally:
            active_path.remove(value_id)
    except RecursionError:
        raise ValueError("model state is invalid") from None


def _canonical(
    model: BaseModel, *, limit: int, exclude: set[str] | None = None
) -> bytes:
    try:
        _exact_model_state(model, type(model))
        payload = json.dumps(
            model.model_dump(mode="json", exclude=exclude or set(), warnings="error"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError):
        raise ValueError("model is invalid") from None
    if len(payload) > limit:
        raise ValueError("model is too large")
    return payload


def _no_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("JSON is invalid")
        result[key] = value
    return result


def _arrays_to_tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_arrays_to_tuples(item) for item in cast(list[object], value))
    if type(value) is dict:
        return {
            key: _arrays_to_tuples(item)
            for key, item in cast(dict[str, object], value).items()
        }
    return value


def _preflight(payload: bytes, *, limit: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= limit:
        raise ValueError("JSON is invalid")
    depth = nodes = 0
    quoted = escaped = False
    for byte in payload:
        if byte > 127:
            raise ValueError("JSON is invalid")
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            elif byte < 32:
                raise ValueError("JSON is invalid")
            continue
        if byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
        elif byte in (125, 93):
            depth -= 1
        if byte not in b" \t\r\n:,":
            nodes += 1
        if depth < 0 or depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("JSON is invalid")
    if quoted or escaped or depth:
        raise ValueError("JSON is invalid")


def _same_shape(original: object, canonical: object) -> bool:
    if type(original) is not type(canonical):
        return False
    if isinstance(original, BaseModel):
        _exact_model_state(original, type(original))
        return all(
            _same_shape(getattr(original, name), getattr(canonical, name))
            for name in original.__class__.model_fields
        )
    if type(original) is tuple:
        candidate = cast(tuple[object, ...], canonical)
        return len(original) == len(candidate) and all(
            _same_shape(left, right)
            for left, right in zip(original, candidate, strict=True)
        )
    return original == canonical


def _parse[T: BaseModel](payload: bytes, expected: type[T], *, limit: int) -> T:
    _preflight(payload, limit=limit)
    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        if type(value) is not dict:
            raise ValueError("JSON is invalid")
        result = expected.model_validate(_arrays_to_tuples(value), strict=True)
        if _canonical(result, limit=limit) != payload:
            raise ValueError("JSON is invalid")
    except (RecursionError, TypeError, UnicodeDecodeError, ValidationError, ValueError):
        raise ValueError("JSON is invalid") from None
    return result


def _strict[T: BaseModel](value: object, expected: type[T], *, limit: int) -> T:
    try:
        _exact_model_state(value, expected)
        rendered = _canonical(cast(BaseModel, value), limit=limit)
        canonical = _parse(rendered, expected, limit=limit)
        if not _same_shape(value, canonical):
            raise ValueError("model is not canonical")
        return canonical
    except RecursionError:
        raise ValueError("model is not canonical") from None


def _hash(domain: bytes, model: BaseModel, *, limit: int) -> str:
    return hashlib.sha256(domain + _canonical(model, limit=limit)).hexdigest()


def _b1_policy_sha256(policy: TargetDeliveryMapProjectionBindingTrustPolicyV1) -> str:
    """Derive the signed-manifest B1 relation-policy identity hash."""

    return hashlib.sha256(
        _B1_POLICY_HASH_DOMAIN
        + target_delivery_map_projection_binding_trust_policy_v1_canonical_json(policy)
    ).hexdigest()


def _sequence(domain: bytes, values: tuple[object, ...]) -> tuple[str, int, int]:
    try:
        encoded = json.dumps(
            values,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError):
        raise ValueError("sequence is invalid") from None
    return hashlib.sha256(domain + encoded).hexdigest(), len(values), len(encoded)


def _safe_absolute_path(value: str) -> str:
    if (
        type(value) is not str
        or not value.isascii()
        or re.fullmatch(_PATH, value) is None
        or "//" in value
        or "/../" in value
        or value.endswith("/..")
        or value.endswith("/")
        or "\\" in value
        or "%" in value
        or any(part in ("", ".", "..") for part in value.split("/")[1:])
    ):
        raise ValueError("path is invalid")
    return value


def _safe_relative_path(value: str) -> str:
    if (
        type(value) is not str
        or not value.isascii()
        or value.startswith("/")
        or "\\" in value
        or "%" in value
        or value.endswith("/")
        or "//" in value
        or re.fullmatch(_RELATIVE_PATH, value) is None
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("path is invalid")
    return value


class TargetDeliveryArtifactManifestRoleInputV2(_Model):
    """Original role artifacts only; no acceptance or derived authority is accepted."""

    schema_version: Literal["rsd.target-delivery-artifact-manifest-role-input.v2"]
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    profile_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4
    projection_binding: TargetDeliveryMapProjectionBindingV1
    phase_a_v5_closure: ContainerBootstrapArtifactEvidenceClosureV5


class TargetDeliveryArtifactManifestV5RolePolicyInputV2(_Model):
    """An explicit caller-pinned V5 policy selection for one manifest role."""

    schema_version: Literal[
        "rsd.target-delivery-artifact-manifest-v5-role-policy-input.v2"
    ]
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    worker_trust_policy: ContainerBootstrapBuildWorkerTrustPolicyV5


class TargetDeliveryArtifactSourceSummaryV2(_Model):
    """The complete common source assertion, without source collection behavior."""

    schema_version: Literal["rsd.target-delivery-artifact-source-summary.v2"]
    repository_identity_sha256: str = Field(pattern=_SHA256)
    git_object_format: Literal["sha1"]
    commit_oid: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_oid: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256)
    wrapper_subtree_path: str = Field(min_length=1, max_length=240)
    wrapper_tree_entries_sha256: str = Field(pattern=_SHA256)
    wrapper_tree_entry_count: int = Field(ge=1, le=32)
    wrapper_tree_entries_byte_count: int = Field(ge=2, le=16_384)
    source_clean: Literal[True]
    untracked_files_absent: Literal[True]
    submodules_absent: Literal[True]
    recipe_sha256: str = Field(pattern=_SHA256)
    toolchain_sha256: str = Field(pattern=_SHA256)
    lock_sha256: str = Field(pattern=_SHA256)
    vendor_sha256: str = Field(pattern=_SHA256)
    builder_recipe_identity_sha256: str = Field(pattern=_SHA256)

    @field_validator(
        "wrapper_tree_entry_count", "wrapper_tree_entries_byte_count", mode="before"
    )
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("source summary is invalid")
        return value

    @field_validator("wrapper_subtree_path")
    @classmethod
    def safe_subtree_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class TargetDeliveryArtifactConfigCommitmentSummaryV2(_Model):
    """Only redacted V5 commitments/summaries for OCI Config.User/WorkingDir/Env."""

    schema_version: Literal["rsd.target-delivery-artifact-config-commitment-summary.v2"]
    oci_config_descriptor_media_type: Literal[
        "application/vnd.oci.image.config.v1+json"
    ]
    oci_config_descriptor_sha256: str = Field(pattern=_SHA256)
    oci_config_descriptor_size_bytes: int = Field(ge=2, le=65_536)
    runtime_uid: int = Field(ge=1, le=2_147_483_647)
    runtime_gid: int = Field(ge=1, le=2_147_483_647)
    user_commitment_sha256: str = Field(pattern=_SHA256)
    user_byte_count: int = Field(ge=3, le=21)
    working_dir_commitment_sha256: str = Field(pattern=_SHA256)
    working_dir_byte_count: int = Field(ge=1, le=240)
    environment_sequence_commitment_sha256: str = Field(pattern=_SHA256)
    environment_names_commitment_sha256: str = Field(pattern=_SHA256)
    environment_entry_count: int = Field(ge=0, le=128)
    environment_rendered_byte_count: int = Field(ge=0, le=32_768)
    reserved_delivery_env_policy_commitment_sha256: str = Field(pattern=_SHA256)
    reserved_delivery_env_names_absent: Literal[True]

    @field_validator(
        "oci_config_descriptor_size_bytes",
        "runtime_uid",
        "runtime_gid",
        "user_byte_count",
        "working_dir_byte_count",
        "environment_entry_count",
        "environment_rendered_byte_count",
        mode="before",
    )
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("config commitment summary is invalid")
        return value

    @field_validator("reserved_delivery_env_names_absent", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("config commitment summary is invalid")
        return value


class TargetDeliveryArtifactOciSummaryV2(_Model):
    """Worker-asserted OCI references and redacted V5 config evidence summary.

    This is a diagnostic aggregation of worker evidence, not a B2-pinned OCI
    namespace or permission to pull, deploy, or otherwise use the reference.
    A later authorization decision owns any repository-use policy.
    """

    schema_version: Literal["rsd.target-delivery-artifact-oci-summary.v2"]
    derived_reference: str = Field(max_length=312)
    index_digest_sha256: str = Field(pattern=_SHA256)
    linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_layer_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_layer_ordinal: int = Field(ge=0, le=31)
    wrapper_path: str = Field(min_length=1, max_length=240)
    wrapper_sha256: str = Field(pattern=_SHA256)
    wrapper_byte_count: int = Field(ge=1, le=67_108_864)
    layer_count: int = Field(ge=1, le=32)
    oci_safe_config_evidence_sha256: str = Field(pattern=_SHA256)
    config: TargetDeliveryArtifactConfigCommitmentSummaryV2

    @field_validator("derived_reference")
    @classmethod
    def canonical_reference(cls, value: str) -> str:
        return validate_oci_repository_reference_v1(value)

    @field_validator("wrapper_path")
    @classmethod
    def safe_wrapper_path(cls, value: str) -> str:
        return _safe_absolute_path(value)

    @field_validator(
        "wrapper_layer_ordinal", "wrapper_byte_count", "layer_count", mode="before"
    )
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("OCI summary is invalid")
        return value

    @model_validator(mode="after")
    def local_oci_coherence(self) -> Self:
        if self.wrapper_layer_ordinal != self.layer_count - 1:
            raise ValueError("OCI summary is invalid")
        return self


class TargetDeliveryArtifactManifestRoleEntryV2(_Model):
    schema_version: Literal["rsd.target-delivery-artifact-manifest-role-entry.v2"]
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    component_role: Literal["infisical", "valkey"]
    ordinal: Literal[0, 1, 2, 3]
    profile_sha256: str = Field(pattern=_SHA256)
    profile_envelope_sha256: str = Field(pattern=_SHA256)
    selected_delivery_route_sha256: str = Field(pattern=_SHA256)
    b1_binding_sha256: str = Field(pattern=_SHA256)
    b1_verification_context_sha256: str = Field(pattern=_SHA256)
    v5_worker_trust_policy_sha256: str = Field(pattern=_SHA256)
    v5_worker_trust_policy_id: str = Field(pattern=_IDENTIFIER)
    v5_worker_trust_policy_epoch: int = Field(ge=1)
    v5_worker_independence_domain_sha256: str = Field(pattern=_SHA256)
    phase_a_v5_closure_sha256: str = Field(pattern=_SHA256)
    phase_a_v5_verification_context_sha256: str = Field(pattern=_SHA256)
    worker_attestation_sha256s: tuple[str, str]
    worker_run_ids: tuple[str, str]
    physical_builder_identity_sha256s: tuple[str, str]
    wrapper_artifact_sha256: str = Field(pattern=_SHA256)
    wrapper_artifact_byte_count: int = Field(ge=1, le=67_108_864)
    wrapper_executable_path: str = Field(min_length=1, max_length=240)
    oci: TargetDeliveryArtifactOciSummaryV2

    @field_validator("worker_attestation_sha256s", "physical_builder_identity_sha256s")
    @classmethod
    def hash_pair_only(cls, value: object) -> tuple[object, object]:
        if type(value) is not tuple or len(value) != 2:
            raise ValueError("manifest role is invalid")
        if any(
            type(item) is not str or re.fullmatch(_SHA256, item) is None
            for item in value
        ):
            raise ValueError("manifest role is invalid")
        return cast(tuple[object, object], value)

    @field_validator("worker_run_ids")
    @classmethod
    def run_id_pair_only(cls, value: object) -> tuple[object, object]:
        if type(value) is not tuple or len(value) != 2:
            raise ValueError("manifest role is invalid")
        if any(
            type(item) is not str or re.fullmatch(_IDENTIFIER, item) is None
            for item in value
        ):
            raise ValueError("manifest role is invalid")
        return cast(tuple[object, object], value)

    @field_validator(
        "v5_worker_trust_policy_epoch", "wrapper_artifact_byte_count", mode="before"
    )
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest role is invalid")
        return value

    @field_validator("wrapper_executable_path")
    @classmethod
    def safe_executable_path(cls, value: str) -> str:
        return _safe_absolute_path(value)

    @model_validator(mode="after")
    def exact_role(self) -> Self:
        if (
            self.component_role != _ROLES[self.component]
            or self.ordinal != _COMPONENTS.index(self.component)
            or len(set(self.worker_run_ids)) != 2
            or len(set(self.worker_attestation_sha256s)) != 2
            or len(set(self.physical_builder_identity_sha256s)) != 2
        ):
            raise ValueError("manifest role is invalid")
        return self


class TargetDeliveryArtifactManifestV2(_Model):
    schema_version: Literal["rsd.target-delivery-artifact-manifest.v2"]
    signature_algorithm: Literal["ed25519"]
    component_order: tuple[
        Literal["primary_infisical"],
        Literal["primary_valkey"],
        Literal["restore_infisical"],
        Literal["restore_valkey"],
    ]
    target_delivery_map_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    map_signer_key_id: str = Field(pattern=_IDENTIFIER)
    map_signer_fingerprint_sha256: str = Field(pattern=_SHA256)
    b1_policy_sha256: str = Field(pattern=_SHA256)
    common_profile_root_key_id: str = Field(pattern=_IDENTIFIER)
    common_profile_root_fingerprint_sha256: str = Field(pattern=_SHA256)
    source: TargetDeliveryArtifactSourceSummaryV2
    # A common, canonical worker assertion only; it is not an OCI authorization root.
    derived_oci_repository: str = Field(max_length=240)
    roles: tuple[
        TargetDeliveryArtifactManifestRoleEntryV2,
        TargetDeliveryArtifactManifestRoleEntryV2,
        TargetDeliveryArtifactManifestRoleEntryV2,
        TargetDeliveryArtifactManifestRoleEntryV2,
    ]
    signer_key_id: str = Field(pattern=_IDENTIFIER)
    signer_fingerprint_sha256: str = Field(pattern=_SHA256)
    signature_base64: str = Field(min_length=4, max_length=128)
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]
    effect_allowed: Literal[False]

    @field_validator("roles", mode="before")
    @classmethod
    def roles_tuple_only(cls, value: object) -> tuple[object, ...]:
        if type(value) is not tuple or len(value) != 4:
            raise ValueError("manifest roles are invalid")
        return cast(tuple[object, ...], value)

    @field_validator("component_order", mode="before")
    @classmethod
    def component_order_tuple_only(cls, value: object) -> tuple[object, ...]:
        if type(value) is not tuple or len(value) != 4:
            raise ValueError("manifest component order is invalid")
        return cast(tuple[object, ...], value)

    @field_validator("derived_oci_repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        return validate_oci_repository_v1(value)

    @field_validator(
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        "effect_allowed",
        mode="before",
    )
    @classmethod
    def exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("manifest is invalid")
        return value

    @model_validator(mode="after")
    def exact_shape(self) -> Self:
        if (
            self.component_order != _COMPONENTS
            or tuple(role.component for role in self.roles) != _COMPONENTS
            or any(
                role.oci.derived_reference
                != oci_repository_reference_v1(
                    self.derived_oci_repository,
                    role.oci.linux_amd64_manifest_digest_sha256,
                )
                for role in self.roles
            )
            or len(_b64(self.signature_base64)) != 64
            or not self.non_authorizing
            or self.evidence_effect_allowed
            or self.build_allowed
            or self.materialization_allowed
            or self.attach_allowed
            or self.effect_allowed
        ):
            raise ValueError("manifest is invalid")
        return self


class TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2(_Model):
    schema_version: Literal[
        "rsd.target-delivery-artifact-manifest-role-acceptance-summary.v2"
    ]
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    profile_sha256: str = Field(pattern=_SHA256)
    b1_binding_sha256: str = Field(pattern=_SHA256)
    v5_worker_trust_policy_sha256: str = Field(pattern=_SHA256)
    phase_a_v5_closure_sha256: str = Field(pattern=_SHA256)


class TargetDeliveryArtifactManifestAcceptanceV2(_Model):
    """Small, non-portable V2 diagnostic; original evidence must be revalidated."""

    schema_version: Literal["rsd.target-delivery-artifact-manifest-acceptance.v2"]
    manifest_sha256: str = Field(pattern=_SHA256)
    verification_context_sha256: str = Field(pattern=_SHA256)
    target_delivery_map_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    b1_policy_sha256: str = Field(pattern=_SHA256)
    roles: tuple[
        TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
        TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
        TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
        TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
    ]
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]
    effect_allowed: Literal[False]

    @field_validator("roles", mode="before")
    @classmethod
    def roles_tuple_only(cls, value: object) -> tuple[object, ...]:
        if type(value) is not tuple or len(value) != 4:
            raise ValueError("manifest acceptance roles are invalid")
        return cast(tuple[object, ...], value)

    @field_validator(
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        "effect_allowed",
        mode="before",
    )
    @classmethod
    def exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("manifest acceptance is invalid")
        return value

    @model_validator(mode="after")
    def exact_roles(self) -> Self:
        if (
            tuple(role.component for role in self.roles) != _COMPONENTS
            or any(
                type(role) is not TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2
                for role in self.roles
            )
            or not self.non_authorizing
            or self.evidence_effect_allowed
            or self.build_allowed
            or self.materialization_allowed
            or self.attach_allowed
            or self.effect_allowed
        ):
            raise ValueError("manifest acceptance is invalid")
        return self


def target_delivery_artifact_manifest_v2_canonical_json(
    manifest: TargetDeliveryArtifactManifestV2,
) -> bytes:
    try:
        return _canonical(
            _strict(
                manifest, TargetDeliveryArtifactManifestV2, limit=_MAX_MANIFEST_BYTES
            ),
            limit=_MAX_MANIFEST_BYTES,
        )
    except (RecursionError, TypeError, ValueError):
        _fail("manifest")


def parse_target_delivery_artifact_manifest_v2_canonical_json(
    payload: bytes,
) -> TargetDeliveryArtifactManifestV2:
    try:
        return _parse(
            payload, TargetDeliveryArtifactManifestV2, limit=_MAX_MANIFEST_BYTES
        )
    except (RecursionError, TypeError, ValueError):
        _fail("parse")


def target_delivery_artifact_manifest_v2_message(
    manifest: TargetDeliveryArtifactManifestV2,
) -> bytes:
    try:
        canonical = _strict(
            manifest, TargetDeliveryArtifactManifestV2, limit=_MAX_MANIFEST_BYTES
        )
        return _MANIFEST_DOMAIN + _canonical(
            canonical, limit=_MAX_MANIFEST_BYTES, exclude={"signature_base64"}
        )
    except (RecursionError, TypeError, ValueError):
        _fail("manifest")


def target_delivery_artifact_manifest_v2_sha256(
    manifest: TargetDeliveryArtifactManifestV2,
) -> str:
    try:
        return hashlib.sha256(
            _MANIFEST_HASH_DOMAIN
            + target_delivery_artifact_manifest_v2_canonical_json(manifest)
        ).hexdigest()
    except (RecursionError, TypeError, ValueError):
        _fail("manifest")


def target_delivery_artifact_manifest_acceptance_v2_canonical_json(
    acceptance: TargetDeliveryArtifactManifestAcceptanceV2,
) -> bytes:
    try:
        return _canonical(
            _strict(
                acceptance,
                TargetDeliveryArtifactManifestAcceptanceV2,
                limit=_MAX_ACCEPTANCE_BYTES,
            ),
            limit=_MAX_ACCEPTANCE_BYTES,
        )
    except (RecursionError, TypeError, ValueError):
        _fail("manifest")


def parse_target_delivery_artifact_manifest_acceptance_v2_canonical_json(
    payload: bytes,
) -> TargetDeliveryArtifactManifestAcceptanceV2:
    try:
        return _parse(
            payload,
            TargetDeliveryArtifactManifestAcceptanceV2,
            limit=_MAX_ACCEPTANCE_BYTES,
        )
    except (RecursionError, TypeError, ValueError):
        _fail("parse")


def _source(
    attestation: ContainerBootstrapArtifactWorkerAttestationV5,
) -> TargetDeliveryArtifactSourceSummaryV2:
    entries = attestation.wrapper_tree_entries
    commitment, count, byte_count = _sequence(
        _SOURCE_TREE_ENTRIES_DOMAIN,
        tuple(entry.model_dump(mode="json", warnings="error") for entry in entries),
    )
    return TargetDeliveryArtifactSourceSummaryV2(
        schema_version="rsd.target-delivery-artifact-source-summary.v2",
        repository_identity_sha256=attestation.canonical_repository_identity_sha256,
        git_object_format=attestation.git_object_format,
        commit_oid=attestation.commit_oid,
        tree_oid=attestation.tree_oid,
        source_snapshot_sha256=attestation.canonical_source_snapshot_sha256,
        wrapper_subtree_path=attestation.wrapper_subtree_path,
        wrapper_tree_entries_sha256=commitment,
        wrapper_tree_entry_count=count,
        wrapper_tree_entries_byte_count=byte_count,
        source_clean=attestation.source_clean,
        untracked_files_absent=attestation.untracked_files_absent,
        submodules_absent=attestation.submodules_absent,
        recipe_sha256=attestation.recipe_sha256,
        toolchain_sha256=attestation.toolchain_sha256,
        lock_sha256=attestation.lock_sha256,
        vendor_sha256=attestation.vendor_sha256,
        builder_recipe_identity_sha256=attestation.builder_recipe_identity_sha256,
    )


def _config(
    claim: PhaseAV5ExpandedOciConfigCommitmentClaimV1,
) -> TargetDeliveryArtifactConfigCommitmentSummaryV2:
    descriptor_digest = claim.oci_config_descriptor_digest
    if (
        claim.oci_config_descriptor_media_type != _OCI_CONFIG_DESCRIPTOR_MEDIA_TYPE
        or not descriptor_digest.startswith("sha256:")
    ):
        raise ValueError("OCI config claim is invalid")
    return TargetDeliveryArtifactConfigCommitmentSummaryV2(
        schema_version="rsd.target-delivery-artifact-config-commitment-summary.v2",
        oci_config_descriptor_media_type=claim.oci_config_descriptor_media_type,
        oci_config_descriptor_sha256=descriptor_digest.removeprefix("sha256:"),
        oci_config_descriptor_size_bytes=claim.oci_config_descriptor_size,
        runtime_uid=claim.runtime_uid,
        runtime_gid=claim.runtime_gid,
        user_commitment_sha256=claim.user_commitment_sha256,
        user_byte_count=claim.user_byte_count,
        working_dir_commitment_sha256=claim.working_dir_commitment_sha256,
        working_dir_byte_count=claim.working_dir_byte_count,
        environment_sequence_commitment_sha256=claim.environment_sequence_commitment_sha256,
        environment_names_commitment_sha256=claim.environment_names_commitment_sha256,
        environment_entry_count=claim.environment_entry_count,
        environment_rendered_byte_count=claim.environment_rendered_byte_count,
        reserved_delivery_env_policy_commitment_sha256=(
            claim.reserved_delivery_env_policy_commitment_sha256
        ),
        reserved_delivery_env_names_absent=claim.reserved_delivery_env_names_absent,
    )


def _oci(
    evidence: ContainerBootstrapOciSafeConfigEvidenceV5,
) -> TargetDeliveryArtifactOciSummaryV2:
    return TargetDeliveryArtifactOciSummaryV2(
        schema_version="rsd.target-delivery-artifact-oci-summary.v2",
        derived_reference=evidence.derived_reference,
        index_digest_sha256=evidence.index_digest_sha256,
        linux_amd64_manifest_digest_sha256=evidence.linux_amd64_manifest_digest_sha256,
        wrapper_layer_digest_sha256=evidence.wrapper_layer_digest_sha256,
        wrapper_layer_ordinal=evidence.wrapper_layer_ordinal,
        wrapper_path=evidence.wrapper_tar_entry.path,
        wrapper_sha256=evidence.wrapper_tar_entry.content_sha256,
        wrapper_byte_count=evidence.wrapper_tar_entry.byte_count,
        layer_count=len(evidence.ordered_layers),
        oci_safe_config_evidence_sha256=container_bootstrap_oci_safe_config_evidence_v5_sha256(
            evidence
        ),
        config=_config(evidence.expanded_oci_config_claim),
    )


def _entry(
    role_input: TargetDeliveryArtifactManifestRoleInputV2,
    *,
    v5_policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
    v5_policy_sha256: str,
    v5_closure_sha256: str,
    v5_context_sha256: str,
    b1_binding_sha256: str,
    b1_context_sha256: str,
) -> TargetDeliveryArtifactManifestRoleEntryV2:
    profile = role_input.profile_envelope.static_role_profile
    first, second = role_input.phase_a_v5_closure.worker_attestations
    return TargetDeliveryArtifactManifestRoleEntryV2(
        schema_version="rsd.target-delivery-artifact-manifest-role-entry.v2",
        component=profile.component,
        component_role=profile.component_role,
        ordinal=cast(Literal[0, 1, 2, 3], _COMPONENTS.index(profile.component)),
        profile_sha256=profile.profile_sha256,
        profile_envelope_sha256=container_bootstrap_static_role_profile_envelope_v4_sha256(
            role_input.profile_envelope
        ),
        selected_delivery_route_sha256=profile.selected_delivery_route_sha256,
        b1_binding_sha256=b1_binding_sha256,
        b1_verification_context_sha256=b1_context_sha256,
        v5_worker_trust_policy_sha256=v5_policy_sha256,
        v5_worker_trust_policy_id=v5_policy.policy_id,
        v5_worker_trust_policy_epoch=v5_policy.epoch,
        v5_worker_independence_domain_sha256=v5_policy.independence_domain_sha256,
        phase_a_v5_closure_sha256=v5_closure_sha256,
        phase_a_v5_verification_context_sha256=v5_context_sha256,
        worker_attestation_sha256s=(
            container_bootstrap_artifact_worker_attestation_v5_sha256(first),
            container_bootstrap_artifact_worker_attestation_v5_sha256(second),
        ),
        worker_run_ids=(first.run_id, second.run_id),
        physical_builder_identity_sha256s=(
            first.physical_builder_identity_sha256,
            second.physical_builder_identity_sha256,
        ),
        wrapper_artifact_sha256=first.wrapper_artifact_sha256,
        wrapper_artifact_byte_count=first.wrapper_artifact_byte_count,
        wrapper_executable_path=first.wrapper_executable_path,
        oci=_oci(role_input.phase_a_v5_closure.oci_safe_config_evidence),
    )


def _check_anchor_separation(
    *,
    manifest_anchor: TargetDeliveryArtifactManifestTrustAnchorV1,
    b1_policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
    role_inputs: tuple[
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
    ],
    role_policies: tuple[
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ],
) -> tuple[str, str, str, str]:
    """Check B2 roots and V5-policy selection without deriving V5 from B1's V4 policy.

    The B1 policy's embedded legacy V4 worker policy participates only in
    collision separation; it never supplies V5 policy provenance.  One
    namespace spans external roots, policy selections, workers, and runs, so
    an identity cannot move between categories to evade a category-local
    uniqueness check.  Identical profile-owned public roots may recur across
    the four immutable profiles.  Physical builders use a separate category:
    they may repeat only as builders across roles, but may not alias any
    protected non-builder identity or hash namespace.
    """

    namespace: set[str] = set()
    builder_values: set[str] = set()

    def register(*values: str) -> None:
        if (
            not values
            or any(type(value) is not str for value in values)
            or len(set(values)) != len(values)
            or any(value in namespace for value in values)
            or any(value in builder_values for value in values)
        ):
            raise ValueError("identity namespace collision")
        namespace.update(values)

    def register_builders(*values: str) -> None:
        """Allow builder reuse, but never builder/non-builder aliases."""

        if (
            not values
            or any(type(value) is not str for value in values)
            or any(value in namespace for value in values)
        ):
            raise ValueError("identity namespace collision")
        builder_values.update(values)

    map_anchor = b1_policy.map_signer_trust_anchor
    binding_anchor = b1_policy.binding_trust_anchor
    profile_anchor = b1_policy.profile_trust_anchor
    legacy_policy = b1_policy.phase_a_worker_trust_policy
    register(
        _b1_policy_sha256(b1_policy),
        b1_policy.policy_id,
        map_anchor.key_id,
        map_anchor.public_key_base64,
        map_anchor.public_key_fingerprint_sha256,
        b1_policy.map_authority_identity_sha256,
        b1_policy.map_independence_domain_identity_sha256,
        binding_anchor.key_id,
        binding_anchor.public_key_base64,
        binding_anchor.public_key_fingerprint_sha256,
        binding_anchor.authority_identity_sha256,
        binding_anchor.independence_domain_identity_sha256,
        profile_anchor.key_id,
        profile_anchor.public_key_base64,
        profile_anchor.public_key_fingerprint_sha256,
        legacy_policy.policy_id,
        legacy_policy.independence_domain_sha256,
    )
    for worker in legacy_policy.worker_trust_anchors:
        register(
            worker.key_id,
            worker.public_key_base64,
            worker.public_key_fingerprint_sha256,
            worker.worker_identity_sha256,
            worker.authority_identity_sha256,
        )
    register(
        manifest_anchor.key_id,
        manifest_anchor.public_key_base64,
        manifest_anchor.public_key_fingerprint_sha256,
        manifest_anchor.authority_identity_sha256,
        manifest_anchor.independence_domain_identity_sha256,
    )

    profile_root_tuples: set[tuple[str, str, str]] = set()
    for role_input in role_inputs:
        profile = role_input.profile_envelope.static_role_profile
        for root in (profile.ticket_trust_anchor, profile.replay_receipt_trust_anchor):
            root_tuple = (
                root.key_id,
                root.public_key_base64,
                root.public_key_fingerprint_sha256,
            )
            if root_tuple not in profile_root_tuples:
                register(*root_tuple)
                profile_root_tuples.add(root_tuple)

    policy_hashes: list[str] = []
    for role_input, item in zip(role_inputs, role_policies, strict=True):
        policy = item.worker_trust_policy
        policy_hash = container_bootstrap_build_worker_trust_policy_v5_sha256(policy)
        policy_hashes.append(policy_hash)
        register(policy_hash, policy.policy_id, policy.independence_domain_sha256)
        for v5_worker in policy.worker_trust_anchors:
            register(
                v5_worker.key_id,
                v5_worker.public_key_base64,
                v5_worker.public_key_fingerprint_sha256,
                v5_worker.worker_identity_sha256,
                v5_worker.authority_identity_sha256,
            )
            register_builders(v5_worker.physical_builder_identity_sha256)
        for attestation in role_input.phase_a_v5_closure.worker_attestations:
            register(attestation.run_id)
    if len(policy_hashes) != 4:
        raise ValueError("V5 policies are not separately pinned")
    return cast(tuple[str, str, str, str], tuple(policy_hashes))


def validate_target_delivery_artifact_manifest_v2(
    *,
    delivery_map: TargetDeliveryMapV1,
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4,
    b1_trust_policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
    manifest_trust_anchor: TargetDeliveryArtifactManifestTrustAnchorV1,
    role_inputs: tuple[
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
    ],
    v5_role_policy_inputs: tuple[
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ],
    manifest: TargetDeliveryArtifactManifestV2,
) -> TargetDeliveryArtifactManifestAcceptanceV2:
    """Revalidate original B1/V5 evidence, reconstruct V2, then verify its signature.

    No caller-supplied acceptance is accepted.  The common B1 policy never
    supplies V5 provenance; V5 validation uses only the four explicit policy
    inputs, in canonical role order.
    """

    try:
        if (
            type(delivery_map) is not TargetDeliveryMapV1
            or type(static_delivery_projection)
            is not ContainerBootstrapStaticDeliveryProjectionV4
            or type(b1_trust_policy)
            is not TargetDeliveryMapProjectionBindingTrustPolicyV1
            or type(manifest_trust_anchor)
            is not TargetDeliveryArtifactManifestTrustAnchorV1
            or type(role_inputs) is not tuple
            or len(role_inputs) != 4
            or any(
                type(item) is not TargetDeliveryArtifactManifestRoleInputV2
                for item in role_inputs
            )
            or type(v5_role_policy_inputs) is not tuple
            or len(v5_role_policy_inputs) != 4
            or any(
                type(item) is not TargetDeliveryArtifactManifestV5RolePolicyInputV2
                for item in v5_role_policy_inputs
            )
        ):
            raise ValueError("input type is invalid")
        _exact_model_state(delivery_map, TargetDeliveryMapV1)
        _exact_model_state(
            static_delivery_projection, ContainerBootstrapStaticDeliveryProjectionV4
        )
        _exact_model_state(
            b1_trust_policy, TargetDeliveryMapProjectionBindingTrustPolicyV1
        )
        static_delivery_projection = (
            parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
                container_bootstrap_static_delivery_projection_v4_canonical_json(
                    static_delivery_projection
                )
            )
        )
        b1_trust_policy = (
            parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
                target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
                    b1_trust_policy
                )
            )
        )
        manifest_trust_anchor = _strict(
            manifest_trust_anchor,
            TargetDeliveryArtifactManifestTrustAnchorV1,
            limit=2_048,
        )
        role_inputs = cast(
            tuple[
                TargetDeliveryArtifactManifestRoleInputV2,
                TargetDeliveryArtifactManifestRoleInputV2,
                TargetDeliveryArtifactManifestRoleInputV2,
                TargetDeliveryArtifactManifestRoleInputV2,
            ],
            tuple(
                _strict(
                    item,
                    TargetDeliveryArtifactManifestRoleInputV2,
                    limit=_MAX_ROLE_INPUT_BYTES,
                )
                for item in role_inputs
            ),
        )
        v5_role_policy_inputs = cast(
            tuple[
                TargetDeliveryArtifactManifestV5RolePolicyInputV2,
                TargetDeliveryArtifactManifestV5RolePolicyInputV2,
                TargetDeliveryArtifactManifestV5RolePolicyInputV2,
                TargetDeliveryArtifactManifestV5RolePolicyInputV2,
            ],
            tuple(
                _strict(
                    item,
                    TargetDeliveryArtifactManifestV5RolePolicyInputV2,
                    limit=_MAX_ROLE_POLICY_BYTES,
                )
                for item in v5_role_policy_inputs
            ),
        )
        manifest = _strict(
            manifest, TargetDeliveryArtifactManifestV2, limit=_MAX_MANIFEST_BYTES
        )
        if (
            tuple(item.component for item in role_inputs) != _COMPONENTS
            or tuple(item.component for item in v5_role_policy_inputs) != _COMPONENTS
        ):
            raise ValueError("role order is invalid")
        v5_policy_hashes = _check_anchor_separation(
            manifest_anchor=manifest_trust_anchor,
            b1_policy=b1_trust_policy,
            role_inputs=role_inputs,
            role_policies=v5_role_policy_inputs,
        )
    except (AttributeError, RecursionError, TypeError, ValueError):
        _fail("anchor")

    entries: list[TargetDeliveryArtifactManifestRoleEntryV2] = []
    source: TargetDeliveryArtifactSourceSummaryV2 | None = None
    repository: str | None = None
    b1_policy_hash = _b1_policy_sha256(b1_trust_policy)
    b1_contexts: list[str] = []
    v5_contexts: list[str] = []
    try:
        for role_input, policy_input, v5_policy_hash in zip(
            role_inputs, v5_role_policy_inputs, v5_policy_hashes, strict=True
        ):
            b1 = validate_target_delivery_map_projection_binding_v1(
                delivery_map=delivery_map,
                static_delivery_projection=static_delivery_projection,
                profile_envelope=role_input.profile_envelope,
                binding=role_input.projection_binding,
                trust_policy=b1_trust_policy,
            )
            v5 = validate_container_bootstrap_artifact_evidence_closure_v5(
                closure=role_input.phase_a_v5_closure,
                worker_trust_policy=policy_input.worker_trust_policy,
                profile_envelope=role_input.profile_envelope,
                profile_trust_anchor=b1_trust_policy.profile_trust_anchor,
            )
            closure_bytes = (
                container_bootstrap_artifact_evidence_closure_v5_canonical_json(
                    role_input.phase_a_v5_closure
                )
            )
            if len(closure_bytes) > _MAX_ROLE_INPUT_BYTES:
                raise ValueError("V5 closure is too large")
            entry = _entry(
                role_input,
                v5_policy=policy_input.worker_trust_policy,
                v5_policy_sha256=v5_policy_hash,
                v5_closure_sha256=v5.closure_sha256,
                v5_context_sha256=v5.verification_context_sha256,
                b1_binding_sha256=b1.binding_sha256,
                b1_context_sha256=b1.verification_context_sha256,
            )
            if (
                role_input.component != entry.component
                or policy_input.component != entry.component
                or entry.component_role != _ROLES[entry.component]
                or entry.oci.derived_reference
                != oci_repository_reference_v1(
                    role_input.phase_a_v5_closure.oci_safe_config_evidence.derived_repository,
                    entry.oci.linux_amd64_manifest_digest_sha256,
                )
            ):
                raise ValueError("role evidence is inconsistent")
            role_source = _source(role_input.phase_a_v5_closure.worker_attestations[0])
            if source is None:
                source = role_source
            elif source != role_source:
                raise ValueError("source is not common")
            role_repository = role_input.phase_a_v5_closure.oci_safe_config_evidence.derived_repository
            if repository is None:
                repository = role_repository
            elif repository != role_repository:
                raise ValueError("OCI repository is not common")
            entries.append(entry)
            b1_contexts.append(b1.verification_context_sha256)
            v5_contexts.append(v5.verification_context_sha256)
        if (
            source is None
            or repository is None
            or len({run_id for entry in entries for run_id in entry.worker_run_ids})
            != 8
        ):
            raise ValueError("missing role evidence")
        expected = TargetDeliveryArtifactManifestV2(
            schema_version="rsd.target-delivery-artifact-manifest.v2",
            signature_algorithm="ed25519",
            component_order=cast(
                tuple[
                    Literal["primary_infisical"],
                    Literal["primary_valkey"],
                    Literal["restore_infisical"],
                    Literal["restore_valkey"],
                ],
                _COMPONENTS,
            ),
            target_delivery_map_sha256=b1.target_delivery_map_sha256,
            static_delivery_projection_sha256=b1.static_delivery_projection_sha256,
            map_signer_key_id=b1_trust_policy.map_signer_trust_anchor.key_id,
            map_signer_fingerprint_sha256=(
                b1_trust_policy.map_signer_trust_anchor.public_key_fingerprint_sha256
            ),
            b1_policy_sha256=b1_policy_hash,
            common_profile_root_key_id=b1_trust_policy.profile_trust_anchor.key_id,
            common_profile_root_fingerprint_sha256=(
                b1_trust_policy.profile_trust_anchor.public_key_fingerprint_sha256
            ),
            source=source,
            derived_oci_repository=repository,
            roles=cast(
                tuple[
                    TargetDeliveryArtifactManifestRoleEntryV2,
                    TargetDeliveryArtifactManifestRoleEntryV2,
                    TargetDeliveryArtifactManifestRoleEntryV2,
                    TargetDeliveryArtifactManifestRoleEntryV2,
                ],
                tuple(entries),
            ),
            signer_key_id=manifest_trust_anchor.key_id,
            signer_fingerprint_sha256=manifest_trust_anchor.public_key_fingerprint_sha256,
            signature_base64=manifest.signature_base64,
            non_authorizing=True,
            evidence_effect_allowed=False,
            build_allowed=False,
            materialization_allowed=False,
            attach_allowed=False,
            effect_allowed=False,
        )
        if manifest != expected:
            raise ValueError("manifest differs from original evidence")
        Ed25519PublicKey.from_public_bytes(
            _b64(manifest_trust_anchor.public_key_base64)
        ).verify(
            _b64(manifest.signature_base64),
            target_delivery_artifact_manifest_v2_message(manifest),
        )
    except (
        InvalidSignature,
        RecursionError,
        TargetDeliveryArtifactManifestV2Error,
        TypeError,
        ValueError,
    ):
        _fail("manifest")

    return TargetDeliveryArtifactManifestAcceptanceV2(
        schema_version="rsd.target-delivery-artifact-manifest-acceptance.v2",
        manifest_sha256=target_delivery_artifact_manifest_v2_sha256(manifest),
        verification_context_sha256=hashlib.sha256(
            _CONTEXT_DOMAIN
            + manifest_trust_anchor.public_key_fingerprint_sha256.encode("ascii")
            + b1_policy_hash.encode("ascii")
            + b"".join(
                policy_hash.encode("ascii")
                + b1_context.encode("ascii")
                + v5_context.encode("ascii")
                for policy_hash, b1_context, v5_context in zip(
                    v5_policy_hashes, b1_contexts, v5_contexts, strict=True
                )
            )
        ).hexdigest(),
        target_delivery_map_sha256=manifest.target_delivery_map_sha256,
        static_delivery_projection_sha256=manifest.static_delivery_projection_sha256,
        b1_policy_sha256=b1_policy_hash,
        roles=cast(
            tuple[
                TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
                TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
                TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
                TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2,
            ],
            tuple(
                TargetDeliveryArtifactManifestRoleAcceptanceSummaryV2(
                    schema_version=(
                        "rsd.target-delivery-artifact-manifest-role-acceptance-summary.v2"
                    ),
                    component=entry.component,
                    profile_sha256=entry.profile_sha256,
                    b1_binding_sha256=entry.b1_binding_sha256,
                    v5_worker_trust_policy_sha256=entry.v5_worker_trust_policy_sha256,
                    phase_a_v5_closure_sha256=entry.phase_a_v5_closure_sha256,
                )
                for entry in entries
            ),
        ),
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
        effect_allowed=False,
    )
