"""Pure, signed Phase-A V5 worker-evidence closure validation.

This module is a verifier for reusable, content-addressed build evidence.  It
does not build, fetch, inspect a registry, read a filesystem, contact a worker,
or create a runtime object.  The two workers' assertions remain assertions:
the local verifier authenticates their signatures and structural bindings only.
``physical_builder_identity_sha256`` is a policy-pinned worker assertion, not
independent proof of physical hardware or builder provenance.

In particular, the V5 safe-config evidence nested here is re-canonicalized and
structurally self-consistent, but its raw config and archive inputs are not
available to this verifier.  Consequently acceptance records worker-attested
internal checks and explicitly records no independent offline re-verification.
Successful validation is non-authorizing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any, Final, Literal, NoReturn, Self, cast

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

from omnimarket.rsd.container_bootstrap_oci_safe_config_evidence_v5 import (
    ContainerBootstrapOciSafeConfigEvidenceV5,
    ContainerBootstrapOciSafeConfigEvidenceV5Error,
    container_bootstrap_oci_safe_config_evidence_v5_sha256,
    strict_canonical_container_bootstrap_oci_safe_config_evidence_v5,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerAttachStaticV4Error,
    ContainerBootstrapStaticProfileTrustAnchorV4,
    ContainerBootstrapStaticRoleProfileEnvelopeV4,
    ContainerBootstrapStaticRoleProfileV4,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    strict_canonical_container_bootstrap_static_profile_trust_anchor_v4,
    verify_container_bootstrap_static_role_profile_envelope_v4,
)

_SHA256: Final = r"^[0-9a-f]{64}$"
_OID: Final = r"^[0-9a-f]{40}$"
_IDENTIFIER: Final = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_PATH: Final = r"^/[A-Za-z0-9._/-]{1,240}$"
_RELATIVE_PATH: Final = r"^[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$"
_MAX_WRAPPER_TREE_ENTRIES: Final = 32
_MAX_POLICY_CANONICAL_BYTES: Final = 8_192
_MAX_ATTESTATION_CANONICAL_BYTES: Final = 98_304
_MAX_CLOSURE_CANONICAL_BYTES: Final = 401_408
_MAX_ACCEPTANCE_CANONICAL_BYTES: Final = 2_048
_MAX_DEPTH: Final = 24
_MAX_NODES: Final = 8_192
_DOMAIN_POLICY_HASH: Final = (
    b"omninode-rsd.container-bootstrap-artifact-worker-trust-policy.sha256.v5\x00"
)
_DOMAIN_ATTESTATION: Final = (
    b"omninode-rsd.container-bootstrap-artifact-worker-attestation.ed25519.v5\x00"
)
_DOMAIN_ATTESTATION_HASH: Final = (
    b"omninode-rsd.container-bootstrap-artifact-worker-attestation.sha256.v5\x00"
)
_DOMAIN_CLOSURE: Final = (
    b"omninode-rsd.container-bootstrap-artifact-evidence-closure.sha256.v5\x00"
)
_DOMAIN_CONTEXT: Final = (
    b"omninode-rsd.container-bootstrap-artifact-evidence-context.sha256.v5\x00"
)
_DOMAIN_ACCEPTANCE_HASH: Final = (
    b"omninode-rsd.container-bootstrap-artifact-evidence-acceptance.sha256.v5\x00"
)
_MISSING_MODEL_STATE: Final = object()
_COMPONENTS: Final = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)


class ContainerBootstrapArtifactEvidenceV5Error(ValueError):
    """The one fixed, redacted public failure for V5 worker evidence."""

    def __init__(self) -> None:
        super().__init__("container bootstrap V5 artifact evidence validation failed")


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail() -> NoReturn:
    raise ContainerBootstrapArtifactEvidenceV5Error()


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


def _items(
    value: object, *, field: str, maximum: int, minimum: int = 1
) -> tuple[object, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} is invalid")
    return cast(tuple[object, ...], value)


def _canonical(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    try:
        canonical = json.dumps(
            model.model_dump(mode="json", exclude=exclude, warnings="error"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("model is not canonical") from None
    if len(canonical) > _canonical_limit(type(model)):
        raise ValueError("model exceeds canonical bound")
    return canonical


def _canonical_limit(model_type: type[BaseModel]) -> int:
    limits = {
        ContainerBootstrapBuildWorkerTrustPolicyV5: _MAX_POLICY_CANONICAL_BYTES,
        ContainerBootstrapArtifactWorkerAttestationV5: _MAX_ATTESTATION_CANONICAL_BYTES,
        ContainerBootstrapArtifactEvidenceClosureV5: _MAX_CLOSURE_CANONICAL_BYTES,
        ContainerBootstrapArtifactEvidenceAcceptanceV5: _MAX_ACCEPTANCE_CANONICAL_BYTES,
    }
    return limits.get(model_type, _MAX_POLICY_CANONICAL_BYTES)


def _same_shape(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return isinstance(right, BaseModel) and all(
            _same_shape(getattr(left, field), getattr(right, field))
            for field in left.__class__.model_fields
        )
    if type(left) is tuple:
        right_tuple = cast(tuple[object, ...], right)
        return len(left) == len(right_tuple) and all(
            _same_shape(item, candidate)
            for item, candidate in zip(left, right_tuple, strict=True)
        )
    return left == right


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


def _validate_tree(
    value: object, *, depth: int = 1, nodes: list[int] | None = None
) -> None:
    if nodes is None:
        nodes = [0]
    if depth > _MAX_DEPTH:
        raise ValueError("JSON is invalid")
    nodes[0] += 1
    if nodes[0] > _MAX_NODES:
        raise ValueError("JSON is invalid")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str:
                raise ValueError("JSON is invalid")
            nodes[0] += 1
            if nodes[0] > _MAX_NODES:
                raise ValueError("JSON is invalid")
            _validate_tree(child, depth=depth + 1, nodes=nodes)
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_tree(child, depth=depth + 1, nodes=nodes)


def _preflight(payload: bytes, *, maximum: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= maximum:
        raise ValueError("JSON is invalid")
    depth = 0
    quoted = False
    escaped = False
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
            if depth > _MAX_DEPTH:
                raise ValueError("JSON is invalid")
        elif byte in (125, 93):
            depth -= 1
            if depth < 0:
                raise ValueError("JSON is invalid")
    if quoted or escaped or depth:
        raise ValueError("JSON is invalid")


def _strict[T: _Model](value: object, expected: type[T]) -> T:
    if type(value) is not expected:
        raise ValueError("concrete type is invalid")
    rendered = _canonical(cast(BaseModel, value))
    try:
        decoded = json.loads(
            rendered.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        _validate_tree(decoded)
        canonical = expected.model_validate(_arrays_to_tuples(decoded), strict=True)
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError, RecursionError):
        raise ValueError("model is invalid") from None
    if _canonical(canonical) != rendered or not _same_shape(value, canonical):
        raise ValueError("model is invalid")
    return canonical


def _parse[T: _Model](payload: bytes, expected: type[T]) -> T:
    _preflight(payload, maximum=_canonical_limit(expected))
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        if type(decoded) is not dict:
            raise ValueError("JSON is invalid")
        _validate_tree(decoded)
        model = expected.model_validate(_arrays_to_tuples(decoded), strict=True)
        model = _strict(model, expected)
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError, RecursionError):
        raise ValueError("JSON is invalid") from None
    if _canonical(model) != payload:
        raise ValueError("JSON is invalid")
    return model


def _hash(domain: bytes, model: BaseModel, *, exclude: set[str] | None = None) -> str:
    return hashlib.sha256(domain + _canonical(model, exclude=exclude)).hexdigest()


def _safe_path(value: str) -> str:
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
    ):
        raise ValueError("path is invalid")
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise ValueError("path is invalid")
    return value


class ContainerBootstrapBuildWorkerTrustAnchorV5(_Model):
    """One policy-pinned V5 worker assertion, including an asserted builder identity.

    The physical-builder digest is selected by the external policy and compared
    to each worker's signed claim.  It is not independent hardware or builder
    provenance proof.
    """

    schema_version: Literal["rsd.container-bootstrap-build-worker-trust-anchor.v5"]
    key_id: str = Field(pattern=_IDENTIFIER)
    worker_identity_sha256: str = Field(pattern=_SHA256)
    authority_identity_sha256: str = Field(pattern=_SHA256)
    physical_builder_identity_sha256: str = Field(pattern=_SHA256)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_key_and_namespaces(self) -> Self:
        key = _b64(self.public_key_base64)
        identities = (
            self.worker_identity_sha256,
            self.authority_identity_sha256,
            self.physical_builder_identity_sha256,
            self.public_key_fingerprint_sha256,
        )
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
        ):
            raise ValueError("worker anchor is invalid")
        if len(set(identities)) != len(identities):
            raise ValueError("worker anchor is invalid")
        return self


class ContainerBootstrapBuildWorkerTrustPolicyV5(_Model):
    """Externally pinned, ordered two-worker V5 trust policy."""

    schema_version: Literal["rsd.container-bootstrap-build-worker-trust-policy.v5"]
    policy_id: str = Field(pattern=_IDENTIFIER)
    epoch: int = Field(ge=1)
    independence_domain_sha256: str = Field(pattern=_SHA256)
    scope: Literal["phase_a_v5_worker_evidence_closure"]
    worker_trust_anchors: tuple[
        ContainerBootstrapBuildWorkerTrustAnchorV5,
        ContainerBootstrapBuildWorkerTrustAnchorV5,
    ]

    @field_validator("epoch", mode="before")
    @classmethod
    def exact_epoch(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("worker policy is invalid")
        return value

    @field_validator("worker_trust_anchors", mode="before")
    @classmethod
    def exactly_two(cls, value: object) -> tuple[object, ...]:
        result = _items(value, field="worker_trust_anchors", maximum=2)
        if len(result) != 2:
            raise ValueError("worker policy is invalid")
        return result

    @model_validator(mode="after")
    def independent_pinned_anchors(self) -> Self:
        first, second = self.worker_trust_anchors
        anchors = (first, second)
        if any(
            type(anchor) is not ContainerBootstrapBuildWorkerTrustAnchorV5
            for anchor in anchors
        ):
            raise ValueError("worker policy is invalid")
        key_ids = (self.policy_id, first.key_id, second.key_id)
        identities = (
            self.independence_domain_sha256,
            first.worker_identity_sha256,
            first.authority_identity_sha256,
            first.physical_builder_identity_sha256,
            first.public_key_fingerprint_sha256,
            second.worker_identity_sha256,
            second.authority_identity_sha256,
            second.physical_builder_identity_sha256,
            second.public_key_fingerprint_sha256,
        )
        if (
            len(set(key_ids)) != len(key_ids)
            or len(set(identities)) != len(identities)
            or first.public_key_base64 == second.public_key_base64
        ):
            raise ValueError("worker policy is invalid")
        return self


class ContainerBootstrapWrapperTreeEntryV5(_Model):
    """One sorted, content-addressed source-tree entry used to build a wrapper."""

    schema_version: Literal["rsd.container-bootstrap-wrapper-tree-entry.v5"]
    path: str = Field(min_length=1, max_length=240)
    object_sha256: str = Field(pattern=_SHA256)
    mode: Literal["0644", "0755"]
    entry_type: Literal["regular"]

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
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
            raise ValueError("tree path is invalid")
        return value


class ContainerBootstrapArtifactWorkerAttestationV5(_Model):
    """One signed worker claim.  It is build evidence, never deployment authority.

    Its physical-builder identity is a policy-pinned worker assertion, not
    independent hardware or builder provenance proof.
    """

    schema_version: Literal["rsd.container-bootstrap-artifact-worker-attestation.v5"]
    policy_id: str = Field(pattern=_IDENTIFIER)
    policy_epoch: int = Field(ge=1)
    worker_trust_policy_sha256: str = Field(pattern=_SHA256)
    independence_domain_sha256: str = Field(pattern=_SHA256)
    signer_key_id: str = Field(pattern=_IDENTIFIER)
    worker_identity_sha256: str = Field(pattern=_SHA256)
    authority_identity_sha256: str = Field(pattern=_SHA256)
    physical_builder_identity_sha256: str = Field(pattern=_SHA256)
    run_id: str = Field(pattern=_IDENTIFIER)
    canonical_repository_identity_sha256: str = Field(pattern=_SHA256)
    git_object_format: Literal["sha1"]
    commit_oid: str = Field(pattern=_OID)
    tree_oid: str = Field(pattern=_OID)
    canonical_source_snapshot_sha256: str = Field(pattern=_SHA256)
    wrapper_subtree_path: str = Field(min_length=1, max_length=240)
    wrapper_tree_entries: tuple[ContainerBootstrapWrapperTreeEntryV5, ...] = Field(
        min_length=1, max_length=_MAX_WRAPPER_TREE_ENTRIES
    )
    source_clean: Literal[True]
    untracked_files_absent: Literal[True]
    submodules_absent: Literal[True]
    recipe_sha256: str = Field(pattern=_SHA256)
    toolchain_sha256: str = Field(pattern=_SHA256)
    lock_sha256: str = Field(pattern=_SHA256)
    vendor_sha256: str = Field(pattern=_SHA256)
    builder_recipe_identity_sha256: str = Field(pattern=_SHA256)
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    component_role: Literal["infisical", "valkey"]
    static_role_profile_sha256: str = Field(pattern=_SHA256)
    profile_envelope_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    selected_delivery_route_sha256: str = Field(pattern=_SHA256)
    static_launch_plan_sha256: str = Field(pattern=_SHA256)
    static_patch_preimage_sha256: str = Field(pattern=_SHA256)
    static_patch_policy_sha256: str = Field(pattern=_SHA256)
    wrapper_artifact_sha256: str = Field(pattern=_SHA256)
    wrapper_artifact_byte_count: int = Field(ge=1, le=67_108_864)
    wrapper_executable_path: str = Field(pattern=_PATH)
    wrapper_uid: Literal[0]
    wrapper_gid: Literal[0]
    wrapper_mode: Literal["0555"]
    wrapper_regular_file: Literal[True]
    wrapper_link_count: Literal[1]
    wrapper_symlink: Literal[False]
    wrapper_hardlink: Literal[False]
    wrapper_setuid: Literal[False]
    wrapper_setgid: Literal[False]
    wrapper_sticky: Literal[False]
    base_image_policy_sha256: str = Field(pattern=_SHA256)
    base_resolution_attestation_sha256: str = Field(pattern=_SHA256)
    base_registry_index_digest_sha256: str = Field(pattern=_SHA256)
    base_linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    base_config_digest_sha256: str = Field(pattern=_SHA256)
    oci_safe_config_evidence_sha256: str = Field(pattern=_SHA256)
    raw_config_internal_consistency_attested_by_worker: Literal[True]
    archive_and_layer_inspection_attested_by_worker: Literal[True]
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]
    signature_base64: str = Field(min_length=4, max_length=128)

    @field_validator("policy_epoch", "wrapper_artifact_byte_count", mode="before")
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("worker attestation is invalid")
        return value

    @field_validator("wrapper_executable_path")
    @classmethod
    def canonical_executable_path(cls, value: str) -> str:
        return _safe_path(value)

    @field_validator("wrapper_subtree_path")
    @classmethod
    def safe_subtree(cls, value: str) -> str:
        return ContainerBootstrapWrapperTreeEntryV5.safe_relative_path(value)

    @field_validator("wrapper_tree_entries", mode="before")
    @classmethod
    def entries_tuple_only(cls, value: object) -> tuple[object, ...]:
        return _items(
            value, field="wrapper_tree_entries", maximum=_MAX_WRAPPER_TREE_ENTRIES
        )

    @field_validator(
        "source_clean",
        "untracked_files_absent",
        "submodules_absent",
        "wrapper_regular_file",
        "wrapper_symlink",
        "wrapper_hardlink",
        "wrapper_setuid",
        "wrapper_setgid",
        "wrapper_sticky",
        "raw_config_internal_consistency_attested_by_worker",
        "archive_and_layer_inspection_attested_by_worker",
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        mode="before",
    )
    @classmethod
    def exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("worker attestation is invalid")
        return value

    @model_validator(mode="after")
    def exact_signed_claim_shape(self) -> Self:
        expected_role = "valkey" if self.component.endswith("valkey") else "infisical"
        entries = self.wrapper_tree_entries
        identities = (
            self.recipe_sha256,
            self.toolchain_sha256,
            self.lock_sha256,
            self.vendor_sha256,
            self.builder_recipe_identity_sha256,
            self.physical_builder_identity_sha256,
            self.worker_identity_sha256,
            self.authority_identity_sha256,
        )
        if (
            len(_b64(self.signature_base64)) != 64
            or self.component_role != expected_role
            or len({entry.path for entry in entries}) != len(entries)
            or tuple(entry.path for entry in entries)
            != tuple(sorted(entry.path for entry in entries))
            or any(
                type(entry) is not ContainerBootstrapWrapperTreeEntryV5
                for entry in entries
            )
            or len(set(identities)) != len(identities)
            or not self.raw_config_internal_consistency_attested_by_worker
            or not self.archive_and_layer_inspection_attested_by_worker
            or not self.non_authorizing
            or self.evidence_effect_allowed
            or self.build_allowed
            or self.materialization_allowed
            or self.attach_allowed
        ):
            raise ValueError("worker attestation is invalid")
        return self


class ContainerBootstrapArtifactEvidenceClosureV5(_Model):
    """Exactly two ordered independent V5 worker attestations for one role."""

    schema_version: Literal["rsd.container-bootstrap-artifact-evidence-closure.v5"]
    worker_attestations: tuple[
        ContainerBootstrapArtifactWorkerAttestationV5,
        ContainerBootstrapArtifactWorkerAttestationV5,
    ]
    oci_safe_config_evidence: ContainerBootstrapOciSafeConfigEvidenceV5
    oci_safe_config_evidence_sha256: str = Field(pattern=_SHA256)
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]

    @field_validator("worker_attestations", mode="before")
    @classmethod
    def exactly_two(cls, value: object) -> tuple[object, ...]:
        result = _items(value, field="worker_attestations", maximum=2)
        if len(result) != 2:
            raise ValueError("closure is invalid")
        return result

    @field_validator(
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        mode="before",
    )
    @classmethod
    def exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("closure is invalid")
        return value

    @model_validator(mode="after")
    def exact_shared_oci_evidence(self) -> Self:
        try:
            checked = strict_canonical_container_bootstrap_oci_safe_config_evidence_v5(
                self.oci_safe_config_evidence
            )
        except ContainerBootstrapOciSafeConfigEvidenceV5Error:
            raise ValueError("closure is invalid") from None
        if (
            type(self.oci_safe_config_evidence)
            is not ContainerBootstrapOciSafeConfigEvidenceV5
            or self.oci_safe_config_evidence_sha256
            != container_bootstrap_oci_safe_config_evidence_v5_sha256(checked)
            or not self.non_authorizing
            or self.evidence_effect_allowed
            or self.build_allowed
            or self.materialization_allowed
            or self.attach_allowed
        ):
            raise ValueError("closure is invalid")
        return self


class ContainerBootstrapArtifactEvidenceAcceptanceV5(_Model):
    """A fixed non-authorizing diagnostic projection of a verified closure.

    ``verification_context_sha256`` is a commitment to supplied verification
    inputs; it is not trust-root authorization.
    """

    schema_version: Literal["rsd.container-bootstrap-artifact-evidence-acceptance.v5"]
    closure_sha256: str = Field(pattern=_SHA256)
    # A context commitment only; it does not authorize a supplied trust root.
    verification_context_sha256: str = Field(pattern=_SHA256)
    raw_config_internal_consistency_attested_by_both_workers: Literal[True]
    archive_and_layer_inspection_attested_by_both_workers: Literal[True]
    raw_config_internal_consistency_independently_reverified: Literal[False]
    archive_and_layer_inspection_independently_reverified: Literal[False]
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]

    @field_validator(
        "raw_config_internal_consistency_attested_by_both_workers",
        "archive_and_layer_inspection_attested_by_both_workers",
        "raw_config_internal_consistency_independently_reverified",
        "archive_and_layer_inspection_independently_reverified",
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        mode="before",
    )
    @classmethod
    def exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("acceptance is invalid")
        return value

    @classmethod
    def model_construct(  # type: ignore[override]
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        """Construct only a complete, exact acceptance record."""
        declared_fields = set(cls.model_fields)
        if (
            set(values) != declared_fields
            or (_fields_set is not None and type(_fields_set) is not set)
            or (_fields_set is not None and _fields_set != declared_fields)
        ):
            raise ValueError("acceptance is invalid")
        return cast(
            Self, super().model_construct(_fields_set=declared_fields, **values)
        )


def _strict_acceptance(
    acceptance: ContainerBootstrapArtifactEvidenceAcceptanceV5,
) -> ContainerBootstrapArtifactEvidenceAcceptanceV5:
    """Reject in-memory state that canonical JSON would otherwise omit."""
    if type(acceptance) is not ContainerBootstrapArtifactEvidenceAcceptanceV5:
        raise ValueError("acceptance is invalid")
    model_state = getattr(acceptance, "__dict__", _MISSING_MODEL_STATE)
    extra_state = getattr(acceptance, "__pydantic_extra__", _MISSING_MODEL_STATE)
    private_state = getattr(acceptance, "__pydantic_private__", _MISSING_MODEL_STATE)
    fields_set = getattr(acceptance, "__pydantic_fields_set__", _MISSING_MODEL_STATE)
    if (
        type(model_state) is not dict
        or set(cast(dict[str, object], model_state))
        != set(ContainerBootstrapArtifactEvidenceAcceptanceV5.model_fields)
        or extra_state is not None
        or private_state is not None
        or type(fields_set) is not set
        or fields_set
        != set(ContainerBootstrapArtifactEvidenceAcceptanceV5.model_fields)
    ):
        raise ValueError("acceptance is invalid")
    return _strict(acceptance, ContainerBootstrapArtifactEvidenceAcceptanceV5)


def strict_canonical_container_bootstrap_build_worker_trust_policy_v5(
    policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
) -> ContainerBootstrapBuildWorkerTrustPolicyV5:
    try:
        return _strict(policy, ContainerBootstrapBuildWorkerTrustPolicyV5)
    except (TypeError, ValueError):
        _fail()


def container_bootstrap_build_worker_trust_policy_v5_canonical_json(
    policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
) -> bytes:
    try:
        return _canonical(_strict(policy, ContainerBootstrapBuildWorkerTrustPolicyV5))
    except (TypeError, ValueError):
        _fail()


def parse_container_bootstrap_build_worker_trust_policy_v5_canonical_json(
    payload: bytes,
) -> ContainerBootstrapBuildWorkerTrustPolicyV5:
    try:
        return _parse(payload, ContainerBootstrapBuildWorkerTrustPolicyV5)
    except ValueError:
        _fail()


def container_bootstrap_build_worker_trust_policy_v5_sha256(
    policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
) -> str:
    try:
        return _hash(
            _DOMAIN_POLICY_HASH,
            _strict(policy, ContainerBootstrapBuildWorkerTrustPolicyV5),
        )
    except (TypeError, ValueError):
        _fail()


def container_bootstrap_artifact_worker_attestation_v5_canonical_json(
    attestation: ContainerBootstrapArtifactWorkerAttestationV5,
) -> bytes:
    try:
        return _canonical(
            _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV5)
        )
    except (TypeError, ValueError):
        _fail()


def parse_container_bootstrap_artifact_worker_attestation_v5_canonical_json(
    payload: bytes,
) -> ContainerBootstrapArtifactWorkerAttestationV5:
    try:
        return _parse(payload, ContainerBootstrapArtifactWorkerAttestationV5)
    except ValueError:
        _fail()


def container_bootstrap_artifact_worker_attestation_v5_message(
    attestation: ContainerBootstrapArtifactWorkerAttestationV5,
) -> bytes:
    try:
        canonical = _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV5)
        return _DOMAIN_ATTESTATION + _canonical(canonical, exclude={"signature_base64"})
    except (TypeError, ValueError):
        _fail()


def container_bootstrap_artifact_worker_attestation_v5_sha256(
    attestation: ContainerBootstrapArtifactWorkerAttestationV5,
) -> str:
    try:
        return _hash(
            _DOMAIN_ATTESTATION_HASH,
            _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV5),
        )
    except (TypeError, ValueError):
        _fail()


def container_bootstrap_artifact_evidence_closure_v5_canonical_json(
    closure: ContainerBootstrapArtifactEvidenceClosureV5,
) -> bytes:
    try:
        return _canonical(_strict(closure, ContainerBootstrapArtifactEvidenceClosureV5))
    except (TypeError, ValueError):
        _fail()


def parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
    payload: bytes,
) -> ContainerBootstrapArtifactEvidenceClosureV5:
    try:
        return _parse(payload, ContainerBootstrapArtifactEvidenceClosureV5)
    except ValueError:
        _fail()


def container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
    acceptance: ContainerBootstrapArtifactEvidenceAcceptanceV5,
) -> bytes:
    try:
        return _canonical(_strict_acceptance(acceptance))
    except (TypeError, ValueError):
        _fail()


def parse_container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
    payload: bytes,
) -> ContainerBootstrapArtifactEvidenceAcceptanceV5:
    try:
        return _strict_acceptance(
            _parse(payload, ContainerBootstrapArtifactEvidenceAcceptanceV5)
        )
    except ValueError:
        _fail()


def container_bootstrap_artifact_evidence_acceptance_v5_sha256(
    acceptance: ContainerBootstrapArtifactEvidenceAcceptanceV5,
) -> str:
    try:
        return _hash(
            _DOMAIN_ACCEPTANCE_HASH,
            _strict_acceptance(acceptance),
        )
    except (TypeError, ValueError):
        _fail()


def _verify_worker(
    attestation: ContainerBootstrapArtifactWorkerAttestationV5,
    anchor: ContainerBootstrapBuildWorkerTrustAnchorV5,
    policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
) -> None:
    try:
        canonical = _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV5)
        exact_anchor = _strict(anchor, ContainerBootstrapBuildWorkerTrustAnchorV5)
        exact_policy = _strict(policy, ContainerBootstrapBuildWorkerTrustPolicyV5)
        if (
            canonical.policy_id != exact_policy.policy_id
            or canonical.policy_epoch != exact_policy.epoch
            or canonical.worker_trust_policy_sha256
            != container_bootstrap_build_worker_trust_policy_v5_sha256(exact_policy)
            or canonical.independence_domain_sha256
            != exact_policy.independence_domain_sha256
            or canonical.signer_key_id != exact_anchor.key_id
            or canonical.worker_identity_sha256 != exact_anchor.worker_identity_sha256
            or canonical.authority_identity_sha256
            != exact_anchor.authority_identity_sha256
            or canonical.physical_builder_identity_sha256
            != exact_anchor.physical_builder_identity_sha256
        ):
            raise ValueError("worker anchor mismatch")
        Ed25519PublicKey.from_public_bytes(_b64(exact_anchor.public_key_base64)).verify(
            _b64(canonical.signature_base64),
            container_bootstrap_artifact_worker_attestation_v5_message(canonical),
        )
    except (
        ContainerBootstrapArtifactEvidenceV5Error,
        InvalidSignature,
        TypeError,
        ValueError,
    ):
        _fail()


def _assert_anchor_separation(
    policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
    profile_anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
    profile: ContainerBootstrapStaticRoleProfileV4,
) -> None:
    first, second = policy.worker_trust_anchors
    if type(profile) is not ContainerBootstrapStaticRoleProfileV4:
        _fail()
    roots = (
        profile_anchor,
        profile.ticket_trust_anchor,
        profile.replay_receipt_trust_anchor,
    )
    if any(not isinstance(root, BaseModel) for root in roots):
        _fail()
    root_key_ids: set[str] = set()
    root_public_keys: set[str] = set()
    root_fingerprints: set[str] = set()
    for root in roots:
        values = cast(BaseModel, root).model_dump(mode="python", warnings="error")
        key_id = values.get("key_id")
        public_key = values.get("public_key_base64")
        fingerprint = values.get("public_key_fingerprint_sha256")
        if (
            type(key_id) is not str
            or type(public_key) is not str
            or type(fingerprint) is not str
        ):
            _fail()
        root_key_ids.add(key_id)
        root_public_keys.add(public_key)
        root_fingerprints.add(fingerprint)
    if (
        len(root_key_ids) != 3
        or len(root_public_keys) != 3
        or len(root_fingerprints) != 3
    ):
        _fail()
    anchors = (first, second)
    identity_namespace = {
        policy.independence_domain_sha256,
        *(anchor.worker_identity_sha256 for anchor in anchors),
        *(anchor.authority_identity_sha256 for anchor in anchors),
        *(anchor.physical_builder_identity_sha256 for anchor in anchors),
        *(anchor.public_key_fingerprint_sha256 for anchor in anchors),
    }
    if (
        len(identity_namespace) != 9
        or policy.policy_id in root_key_ids
        or {anchor.key_id for anchor in anchors} & root_key_ids
        or {anchor.public_key_base64 for anchor in anchors} & root_public_keys
        or identity_namespace & root_fingerprints
    ):
        _fail()


def _assert_pair_agreement(
    first: ContainerBootstrapArtifactWorkerAttestationV5,
    second: ContainerBootstrapArtifactWorkerAttestationV5,
) -> None:
    different = {
        "signer_key_id",
        "worker_identity_sha256",
        "authority_identity_sha256",
        "physical_builder_identity_sha256",
        "run_id",
        "signature_base64",
    }
    if first.run_id == second.run_id:
        _fail()
    for field in first.__class__.model_fields:
        if field not in different and not _same_shape(
            getattr(first, field), getattr(second, field)
        ):
            _fail()


def validate_container_bootstrap_artifact_evidence_closure_v5(
    *,
    closure: ContainerBootstrapArtifactEvidenceClosureV5,
    worker_trust_policy: ContainerBootstrapBuildWorkerTrustPolicyV5,
    profile_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
    profile_trust_anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
) -> ContainerBootstrapArtifactEvidenceAcceptanceV5:
    """Validate one non-authorizing V5 closure against supplied pinned inputs.

    The profile envelope and V5 policy are externally supplied and canonicalized
    before either worker claim is used. This function does not authenticate
    ``worker_trust_policy`` or ``profile_trust_anchor``; callers MUST obtain
    and pin both out of band. No freshness decision is made here;
    callers choose a currently pinned policy outside this reusable artifact
    evidence domain.
    """

    try:
        checked = _strict(closure, ContainerBootstrapArtifactEvidenceClosureV5)
        policy = _strict(
            worker_trust_policy, ContainerBootstrapBuildWorkerTrustPolicyV5
        )
        profile = verify_container_bootstrap_static_role_profile_envelope_v4(
            envelope=profile_envelope, profile_trust_anchor=profile_trust_anchor
        )
        canonical_profile_anchor = (
            strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(
                profile_trust_anchor
            )
        )
        envelope_sha256 = container_bootstrap_static_role_profile_envelope_v4_sha256(
            profile_envelope
        )
    except (
        ContainerAttachStaticV4Error,
        ContainerBootstrapArtifactEvidenceV5Error,
        TypeError,
        ValueError,
    ):
        _fail()
    _assert_anchor_separation(policy, canonical_profile_anchor, profile)
    first_anchor, second_anchor = policy.worker_trust_anchors
    first, second = checked.worker_attestations
    _verify_worker(first, first_anchor, policy)
    _verify_worker(second, second_anchor, policy)
    _assert_pair_agreement(first, second)
    try:
        checked_oci = strict_canonical_container_bootstrap_oci_safe_config_evidence_v5(
            checked.oci_safe_config_evidence
        )
    except ContainerBootstrapOciSafeConfigEvidenceV5Error:
        _fail()
    launch = profile.static_launch_plan
    if (
        first.component not in _COMPONENTS
        or first.component != profile.component
        or first.component_role != profile.component_role
        or first.static_role_profile_sha256 != profile.profile_sha256
        or first.profile_envelope_sha256 != envelope_sha256
        or first.static_delivery_projection_sha256
        != profile.static_delivery_projection_sha256
        or first.selected_delivery_route_sha256
        != profile.selected_delivery_route_sha256
        or first.static_launch_plan_sha256 != profile.static_launch_plan_sha256
        or first.static_patch_preimage_sha256 != profile.static_patch_preimage_sha256
        or first.static_patch_policy_sha256 != profile.static_patch_policy_sha256
        or first.canonical_source_snapshot_sha256
        != profile.static_patch_preimage.wrapper_source_tree_sha256
        or first.wrapper_executable_path != profile.wrapper_executable_path
        or first.wrapper_executable_path != launch.wrapper_executable_path
        or first.base_image_policy_sha256 != launch.base_image_policy_sha256
        or first.base_resolution_attestation_sha256
        != launch.base_resolution_attestation_sha256
        or first.base_registry_index_digest_sha256
        != launch.base_registry_index_digest_sha256
        or first.base_linux_amd64_manifest_digest_sha256
        != launch.base_linux_amd64_manifest_digest_sha256
        or first.base_config_digest_sha256 != launch.base_config_digest_sha256
        or checked.oci_safe_config_evidence_sha256
        != container_bootstrap_oci_safe_config_evidence_v5_sha256(checked_oci)
        or first.oci_safe_config_evidence_sha256
        != checked.oci_safe_config_evidence_sha256
        or checked_oci.entrypoint != launch.wrapper_argv_prefix + launch.base_entrypoint
        or checked_oci.cmd != launch.base_command
        or first.wrapper_artifact_sha256 != checked_oci.wrapper_tar_entry.content_sha256
        or first.wrapper_artifact_byte_count != checked_oci.wrapper_tar_entry.byte_count
        or first.wrapper_executable_path != checked_oci.wrapper_tar_entry.path
        or not first.raw_config_internal_consistency_attested_by_worker
        or not first.archive_and_layer_inspection_attested_by_worker
        or not first.non_authorizing
        or first.evidence_effect_allowed
        or first.build_allowed
        or first.materialization_allowed
        or first.attach_allowed
    ):
        _fail()
    return ContainerBootstrapArtifactEvidenceAcceptanceV5(
        schema_version="rsd.container-bootstrap-artifact-evidence-acceptance.v5",
        closure_sha256=_hash(_DOMAIN_CLOSURE, checked),
        verification_context_sha256=hashlib.sha256(
            _DOMAIN_CONTEXT
            + canonical_profile_anchor.public_key_fingerprint_sha256.encode("ascii")
            + _canonical(policy)
        ).hexdigest(),
        raw_config_internal_consistency_attested_by_both_workers=True,
        archive_and_layer_inspection_attested_by_both_workers=True,
        raw_config_internal_consistency_independently_reverified=False,
        archive_and_layer_inspection_independently_reverified=False,
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
