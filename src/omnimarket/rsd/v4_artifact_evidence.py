"""Pure, signed V4 wrapper-artifact and OCI evidence validation.

Faithfully extracted from immutable public RSD commit
``fcd55535cf8199b1b1098f417f56349f1301ba05``.

This module is intentionally a verifier, not a collector or build API.  A
source/commit/tree assertion is a signed worker statement; it is never local
proof that Git, a filesystem, a registry, or an image engine was consulted.
Successful validation is non-authorizing: all effect, build, materialization,
and attach permissions remain false.
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
    ValidationInfo,
    field_validator,
    model_validator,
)

from omnimarket.rsd.oci_repository import (
    oci_repository_reference_v1,
    validate_oci_repository_reference_v1,
    validate_oci_repository_v1,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticProfileTrustAnchorV4,
    ContainerBootstrapStaticRoleProfileEnvelopeV4,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    strict_canonical_container_bootstrap_static_profile_trust_anchor_v4,
    verify_container_bootstrap_static_role_profile_envelope_v4,
)

_SHA256 = r"^[0-9a-f]{64}$"
_OID = r"^[0-9a-f]{40}$"
_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_PATH = r"^/[A-Za-z0-9._/-]{1,240}$"
_RELATIVE_PATH = r"^[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$"
_STATIC_V4_ARG = r"^[A-Za-z0-9._/:=@+,%=-]{1,256}$"
_MAX_OCI_LAYERS = 32
_MAX_STATIC_V4_ARG_ITEMS = 64
_MAX_STATIC_V4_ARG_BYTES = 256
_MAX_OCI_ENTRYPOINT_ITEMS = _MAX_STATIC_V4_ARG_ITEMS * 2
_MAX_OCI_ENTRYPOINT_BYTES = _MAX_STATIC_V4_ARG_ITEMS * _MAX_STATIC_V4_ARG_BYTES * 2
_MAX_OCI_CMD_ITEMS = _MAX_STATIC_V4_ARG_ITEMS
_MAX_OCI_CMD_BYTES = _MAX_STATIC_V4_ARG_ITEMS * _MAX_STATIC_V4_ARG_BYTES
_MAX_OCI_INDEX_JSON_BYTES = 8_192
_MAX_OCI_MANIFEST_JSON_BYTES = 8_192
_MAX_OCI_CONFIG_JSON_BYTES = 65_536
_MAX_OCI_INDEX_BASE64_CHARS = ((_MAX_OCI_INDEX_JSON_BYTES + 2) // 3) * 4
_MAX_OCI_MANIFEST_BASE64_CHARS = ((_MAX_OCI_MANIFEST_JSON_BYTES + 2) // 3) * 4
_MAX_OCI_CONFIG_BASE64_CHARS = ((_MAX_OCI_CONFIG_JSON_BYTES + 2) // 3) * 4
_MAX_WRAPPER_TREE_ENTRIES = 32
_MAX_POLICY_CANONICAL_BYTES = 8_192
_MAX_OCI_EVIDENCE_CANONICAL_BYTES = 196_608
_MAX_ATTESTATION_CANONICAL_BYTES = 196_608
_MAX_CLOSURE_CANONICAL_BYTES = 393_216
_MAX_ACCEPTANCE_CANONICAL_BYTES = 2_048
_MAX_DEPTH = 24
_MAX_NODES = 4096
_DOMAIN_ATTESTATION = (
    b"omninode-rsd.container-bootstrap-artifact-worker-attestation.ed25519.v4\x00"
)
_DOMAIN_ATTESTATION_HASH = (
    b"omninode-rsd.container-bootstrap-artifact-worker-attestation.sha256.v4\x00"
)
_DOMAIN_CLOSURE = (
    b"omninode-rsd.container-bootstrap-artifact-evidence-closure.sha256.v4\x00"
)
_DOMAIN_CONTEXT = (
    b"omninode-rsd.container-bootstrap-artifact-evidence-context.sha256.v4\x00"
)


class ContainerBootstrapArtifactEvidenceV4Error(ValueError):
    """Fixed public failure for this isolated, non-authorizing evidence domain."""

    __slots__ = ("phase",)

    def __init__(
        self, phase: Literal["parse", "profile", "anchor", "signature", "binding"]
    ):
        super().__init__("container bootstrap V4 artifact evidence validation failed")
        self.phase = phase


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail(
    phase: Literal["parse", "profile", "anchor", "signature", "binding"],
) -> NoReturn:
    raise ContainerBootstrapArtifactEvidenceV4Error(phase)


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


def _items(value: object, *, field: str, max_items: int) -> tuple[object, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= max_items:
        raise ValueError(f"{field} is invalid")
    return cast(tuple[object, ...], value)


def _canonical(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    try:
        canonical = json.dumps(
            model.model_dump(mode="json", exclude=exclude, warnings="error"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("model is not canonical") from None
    if len(canonical) > _canonical_limit(type(model)):
        raise ValueError("model exceeds its canonical bound")
    return canonical


def _canonical_limit(model_type: type[BaseModel]) -> int:
    """Return the finite public canonical bound for every serializable V4 type."""

    limits = {
        ContainerBootstrapBuildWorkerTrustPolicyV4: _MAX_POLICY_CANONICAL_BYTES,
        ContainerBootstrapOciEvidenceV4: _MAX_OCI_EVIDENCE_CANONICAL_BYTES,
        ContainerBootstrapArtifactWorkerAttestationV4: _MAX_ATTESTATION_CANONICAL_BYTES,
        ContainerBootstrapArtifactEvidenceClosureV4: _MAX_CLOSURE_CANONICAL_BYTES,
        ContainerBootstrapArtifactEvidenceAcceptanceV4: _MAX_ACCEPTANCE_CANONICAL_BYTES,
    }
    return limits.get(model_type, _MAX_OCI_MANIFEST_JSON_BYTES)


def _same_shape(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return isinstance(right, BaseModel) and all(
            _same_shape(getattr(left, key), getattr(right, key))
            for key in left.__class__.model_fields
        )
    if type(left) is tuple:
        return len(left) == len(cast(tuple[object, ...], right)) and all(
            _same_shape(a, b)
            for a, b in zip(left, cast(tuple[object, ...], right), strict=True)
        )
    return left == right


def _strict[T: _Model](value: object, expected: type[T]) -> T:
    if type(value) is not expected:
        raise ValueError("concrete type is invalid")
    rendered = _canonical(cast(BaseModel, value))
    try:
        decoded = json.loads(rendered.decode("ascii"), object_pairs_hook=_no_duplicates)
        canonical = expected.model_validate(_arrays_to_tuples(decoded), strict=True)
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError):
        raise ValueError("model is invalid") from None
    if _canonical(canonical) != rendered or not _same_shape(value, canonical):
        raise ValueError("model is invalid")
    return canonical


def _no_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("JSON is invalid")
        result[key] = value
    return result


def _arrays_to_tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_arrays_to_tuples(item) for item in value)
    if type(value) is dict:
        return {key: _arrays_to_tuples(item) for key, item in value.items()}
    return value


def _preflight(payload: bytes, *, max_bytes: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= max_bytes:
        raise ValueError("JSON is invalid")
    depth = nodes = 0
    quote = escape = False
    for char in payload:
        if quote:
            if escape:
                escape = False
            elif char == 92:
                escape = True
            elif char == 34:
                quote = False
            elif char < 32:
                raise ValueError("JSON is invalid")
            continue
        if char == 34:
            quote = True
            nodes += 1
        elif char in (123, 91):
            depth += 1
            nodes += 1
        elif char in (125, 93):
            depth -= 1
        elif char not in b" \t\r\n:,":
            nodes += 1
        if depth < 0 or depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("JSON is invalid")
    if quote or escape or depth:
        raise ValueError("JSON is invalid")


def _parse[T: _Model](payload: bytes, expected: type[T]) -> T:
    _preflight(payload, max_bytes=_canonical_limit(expected))
    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
        )
    except (UnicodeDecodeError, TypeError, ValueError):
        raise ValueError("JSON is invalid") from None
    if type(value) is not dict:
        raise ValueError("JSON is invalid")
    try:
        model = expected.model_validate(_arrays_to_tuples(value), strict=True)
        model = _strict(model, expected)
    except (TypeError, ValidationError, ValueError):
        raise ValueError("JSON is invalid") from None
    if _canonical(model) != payload:
        raise ValueError("JSON is invalid")
    return model


def _hash(domain: bytes, model: BaseModel, *, exclude: set[str] | None = None) -> str:
    return hashlib.sha256(domain + _canonical(model, exclude=exclude)).hexdigest()


def _safe_path(value: str) -> str:
    if (
        type(value) is not str
        or re.fullmatch(_PATH, value) is None
        or "//" in value
        or "/../" in value
        or value.endswith("/..")
        or value.endswith("/")
        or "\\" in value
        or "%" in value
    ):
        raise ValueError("path is invalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts[1:]):
        raise ValueError("path is invalid")
    return value


def _canonical_json_object(payload: bytes, *, max_bytes: int) -> dict[str, object]:
    _preflight(payload, max_bytes=max_bytes)
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
        )
    except (UnicodeDecodeError, TypeError, ValueError):
        raise ValueError("OCI JSON is invalid") from None
    if type(decoded) is not dict:
        raise ValueError("OCI JSON is invalid")
    if (
        json.dumps(
            decoded, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        != payload
    ):
        raise ValueError("OCI JSON is invalid")
    return cast(dict[str, object], decoded)


class ContainerBootstrapBuildWorkerTrustAnchorV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-build-worker-trust-anchor.v4"]
    key_id: str = Field(pattern=_IDENTIFIER)
    worker_identity_sha256: str = Field(pattern=_SHA256)
    authority_identity_sha256: str = Field(pattern=_SHA256)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_key(self) -> Self:
        key = _b64(self.public_key_base64)
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
            or self.worker_identity_sha256 == self.authority_identity_sha256
            or self.worker_identity_sha256 == self.public_key_fingerprint_sha256
            or self.authority_identity_sha256 == self.public_key_fingerprint_sha256
        ):
            raise ValueError("worker anchor is invalid")
        return self


class ContainerBootstrapBuildWorkerTrustPolicyV4(_Model):
    """Externally pinned, immutable two-worker root policy for one validation call."""

    schema_version: Literal["rsd.container-bootstrap-build-worker-trust-policy.v4"]
    policy_id: str = Field(pattern=_IDENTIFIER)
    independence_domain_sha256: str = Field(pattern=_SHA256)
    worker_trust_anchors: tuple[
        ContainerBootstrapBuildWorkerTrustAnchorV4,
        ContainerBootstrapBuildWorkerTrustAnchorV4,
    ]

    @field_validator("worker_trust_anchors", mode="before")
    @classmethod
    def exactly_two(cls, value: object) -> tuple[object, ...]:
        result = _items(value, field="worker_trust_anchors", max_items=2)
        if len(result) != 2:
            raise ValueError("worker policy is invalid")
        return result

    @model_validator(mode="after")
    def separate_roots(self) -> Self:
        first, second = self.worker_trust_anchors
        identities = (
            self.independence_domain_sha256,
            first.worker_identity_sha256,
            first.authority_identity_sha256,
            second.worker_identity_sha256,
            second.authority_identity_sha256,
            first.public_key_fingerprint_sha256,
            second.public_key_fingerprint_sha256,
        )
        if (
            type(first) is not ContainerBootstrapBuildWorkerTrustAnchorV4
            or type(second) is not ContainerBootstrapBuildWorkerTrustAnchorV4
            or len({self.policy_id, first.key_id, second.key_id}) != 3
            or len(set(identities)) != len(identities)
            or first.public_key_base64 == second.public_key_base64
        ):
            raise ValueError("worker policy is invalid")
        return self


class ContainerBootstrapWrapperTreeEntryV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-wrapper-tree-entry.v4"]
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
        ):
            raise ValueError("tree path is invalid")
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("tree path is invalid")
        return value


class ContainerBootstrapOciLayerDescriptorV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-oci-layer-descriptor.v4"]
    media_type: Literal["application/vnd.oci.image.layer.v1.tar+gzip"]
    digest_sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=1, le=1_073_741_824)
    diff_id_sha256: str = Field(pattern=_SHA256)


class ContainerBootstrapOciWrapperEntryV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-oci-wrapper-entry.v4"]
    path: str = Field(pattern=_PATH)
    uid: Literal[0]
    gid: Literal[0]
    mode: Literal["0555"]
    entry_type: Literal["regular"]
    link_count: Literal[1]
    symlink: Literal[False]
    hardlink: Literal[False]
    setuid: Literal[False]
    setgid: Literal[False]
    sticky: Literal[False]
    content_sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=1, le=67_108_864)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_path(value)


class ContainerBootstrapWrapperArchiveInspectionV4(_Model):
    """A signed exhaustive archive-inspection claim; collection is out of scope."""

    schema_version: Literal["rsd.container-bootstrap-wrapper-archive-inspection.v4"]
    archive_entry_count: int = Field(ge=1, le=65_536)
    inspected_layer_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_path: str = Field(pattern=_PATH)
    wrapper_matching_entry_count: Literal[1]
    duplicate_names_absent: Literal[True]
    pax_headers_absent: Literal[True]
    gnu_longname_or_link_absent: Literal[True]
    traversal_paths_absent: Literal[True]
    absolute_paths_absent: Literal[True]
    whiteout_entries_absent: Literal[True]
    privilege_bits_absent: Literal[True]
    symlink_entries_absent: Literal[True]
    hardlink_entries_absent: Literal[True]
    device_fifo_socket_entries_absent: Literal[True]
    sparse_entries_absent: Literal[True]
    nonregular_entries_absent: Literal[True]
    trailing_conflicting_wrapper_entries_absent: Literal[True]

    @field_validator("wrapper_path")
    @classmethod
    def canonical_wrapper_path(cls, value: str) -> str:
        return _safe_path(value)


class ContainerBootstrapOciEvidenceV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-oci-evidence.v4"]
    derived_repository: str = Field(max_length=240)
    derived_reference: str = Field(max_length=312)
    base_image_policy_sha256: str = Field(pattern=_SHA256)
    base_resolution_attestation_sha256: str = Field(pattern=_SHA256)
    base_registry_index_digest_sha256: str = Field(pattern=_SHA256)
    base_linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    base_config_digest_sha256: str = Field(pattern=_SHA256)
    index_digest_sha256: str = Field(pattern=_SHA256)
    linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    config_digest_sha256: str = Field(pattern=_SHA256)
    index_canonical_json_sha256: str = Field(pattern=_SHA256)
    index_canonical_json_byte_count: int = Field(ge=2, le=_MAX_OCI_INDEX_JSON_BYTES)
    index_canonical_json_utf8_base64: str = Field(
        min_length=4, max_length=_MAX_OCI_INDEX_BASE64_CHARS
    )
    selected_manifest_descriptor_digest_sha256: str = Field(pattern=_SHA256)
    manifest_canonical_json_sha256: str = Field(pattern=_SHA256)
    manifest_canonical_json_byte_count: int = Field(
        ge=2, le=_MAX_OCI_MANIFEST_JSON_BYTES
    )
    manifest_canonical_json_utf8_base64: str = Field(
        min_length=4, max_length=_MAX_OCI_MANIFEST_BASE64_CHARS
    )
    manifest_config_descriptor_digest_sha256: str = Field(pattern=_SHA256)
    config_canonical_json_sha256: str = Field(pattern=_SHA256)
    config_canonical_json_byte_count: int = Field(ge=2, le=_MAX_OCI_CONFIG_JSON_BYTES)
    config_canonical_json_utf8_base64: str = Field(
        min_length=4, max_length=_MAX_OCI_CONFIG_BASE64_CHARS
    )
    platform_os: Literal["linux"]
    platform_architecture: Literal["amd64"]
    ordered_layers: tuple[ContainerBootstrapOciLayerDescriptorV4, ...] = Field(
        min_length=1, max_length=_MAX_OCI_LAYERS
    )
    config_rootfs_diff_ids_sha256: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_OCI_LAYERS
    )
    wrapper_layer_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_layer_ordinal: int = Field(ge=0, le=_MAX_OCI_LAYERS - 1)
    wrapper_tar_entry: ContainerBootstrapOciWrapperEntryV4
    wrapper_archive_inspection: ContainerBootstrapWrapperArchiveInspectionV4
    entrypoint: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_OCI_ENTRYPOINT_ITEMS
    )
    cmd: tuple[str, ...] = Field(max_length=_MAX_OCI_CMD_ITEMS)

    @field_validator(
        "ordered_layers",
        "config_rootfs_diff_ids_sha256",
        "entrypoint",
        "cmd",
        mode="before",
    )
    @classmethod
    def tuple_only(cls, value: object, info: ValidationInfo) -> tuple[object, ...]:
        if info.field_name == "cmd" and type(value) is tuple and not value:
            return value
        max_items = {
            "ordered_layers": _MAX_OCI_LAYERS,
            "config_rootfs_diff_ids_sha256": _MAX_OCI_LAYERS,
            "entrypoint": _MAX_OCI_ENTRYPOINT_ITEMS,
            "cmd": _MAX_OCI_CMD_ITEMS,
        }.get(str(info.field_name))
        if max_items is None:
            raise ValueError("OCI sequence is invalid")
        return _items(value, field=str(info.field_name), max_items=max_items)

    @field_validator("derived_repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        return validate_oci_repository_v1(value)

    @field_validator("derived_reference")
    @classmethod
    def canonical_reference(cls, value: str) -> str:
        return validate_oci_repository_reference_v1(value)

    @field_validator("entrypoint", "cmd")
    @classmethod
    def bounded_argv(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        max_total = (
            _MAX_OCI_ENTRYPOINT_BYTES
            if info.field_name == "entrypoint"
            else _MAX_OCI_CMD_BYTES
        )
        if (
            any(
                type(item) is not str or re.fullmatch(_STATIC_V4_ARG, item) is None
                for item in value
            )
            or sum(len(item.encode("ascii")) for item in value) > max_total
        ):
            raise ValueError("OCI argv is invalid")
        return value

    @model_validator(mode="after")
    def exact_oci(self) -> Self:
        expected_reference = oci_repository_reference_v1(
            self.derived_repository, self.linux_amd64_manifest_digest_sha256
        )
        layer_digests = tuple(layer.digest_sha256 for layer in self.ordered_layers)
        diff_ids = tuple(layer.diff_id_sha256 for layer in self.ordered_layers)
        try:
            index_bytes = _b64(self.index_canonical_json_utf8_base64)
            manifest_bytes = _b64(self.manifest_canonical_json_utf8_base64)
            config_bytes = _b64(self.config_canonical_json_utf8_base64)
            index = _canonical_json_object(
                index_bytes, max_bytes=_MAX_OCI_INDEX_JSON_BYTES
            )
            manifest = _canonical_json_object(
                manifest_bytes, max_bytes=_MAX_OCI_MANIFEST_JSON_BYTES
            )
            config = _canonical_json_object(
                config_bytes, max_bytes=_MAX_OCI_CONFIG_JSON_BYTES
            )
        except ValueError:
            raise ValueError("OCI evidence is invalid") from None
        selected = {
            "digest": f"sha256:{self.linux_amd64_manifest_digest_sha256}",
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "platform": {"architecture": "amd64", "os": "linux"},
            "size": self.manifest_canonical_json_byte_count,
        }
        layers = [
            {
                "digest": f"sha256:{layer.digest_sha256}",
                "mediaType": layer.media_type,
                "size": layer.byte_count,
            }
            for layer in self.ordered_layers
        ]
        documents_match = (
            len(index_bytes) == self.index_canonical_json_byte_count
            and len(manifest_bytes) == self.manifest_canonical_json_byte_count
            and len(config_bytes) == self.config_canonical_json_byte_count
            and hashlib.sha256(index_bytes).hexdigest()
            == self.index_canonical_json_sha256
            and hashlib.sha256(manifest_bytes).hexdigest()
            == self.manifest_canonical_json_sha256
            and hashlib.sha256(config_bytes).hexdigest()
            == self.config_canonical_json_sha256
            and self.index_digest_sha256 == self.index_canonical_json_sha256
            and self.linux_amd64_manifest_digest_sha256
            == self.manifest_canonical_json_sha256
            and self.config_digest_sha256 == self.config_canonical_json_sha256
            and index
            == {
                "manifests": [selected],
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "schemaVersion": 2,
            }
            and manifest
            == {
                "config": {
                    "digest": f"sha256:{self.config_digest_sha256}",
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "size": self.config_canonical_json_byte_count,
                },
                "layers": layers,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "schemaVersion": 2,
            }
            and config
            == {
                "architecture": "amd64",
                "config": {"Cmd": list(self.cmd), "Entrypoint": list(self.entrypoint)},
                "os": "linux",
                "rootfs": {
                    "diff_ids": [
                        f"sha256:{item}" for item in self.config_rootfs_diff_ids_sha256
                    ],
                    "type": "layers",
                },
            }
        )
        if (
            type(self.wrapper_tar_entry) is not ContainerBootstrapOciWrapperEntryV4
            or type(self.wrapper_archive_inspection)
            is not ContainerBootstrapWrapperArchiveInspectionV4
            or not documents_match
            or self.derived_reference != expected_reference
            or self.selected_manifest_descriptor_digest_sha256
            != self.linux_amd64_manifest_digest_sha256
            or self.manifest_config_descriptor_digest_sha256
            != self.config_digest_sha256
            or self.wrapper_layer_digest_sha256 not in layer_digests
            or len(set(layer_digests)) != len(layer_digests)
            or len(set(diff_ids)) != len(diff_ids)
            or self.wrapper_layer_ordinal >= len(self.ordered_layers)
            or self.wrapper_layer_ordinal != len(self.ordered_layers) - 1
            or self.ordered_layers[self.wrapper_layer_ordinal].digest_sha256
            != self.wrapper_layer_digest_sha256
            or self.wrapper_archive_inspection.inspected_layer_digest_sha256
            != self.wrapper_layer_digest_sha256
            or self.wrapper_archive_inspection.wrapper_path
            != self.wrapper_tar_entry.path
            or self.config_rootfs_diff_ids_sha256 != diff_ids
            or any(
                type(value) is not str or re.fullmatch(_SHA256, value) is None
                for value in self.config_rootfs_diff_ids_sha256
            )
            or type(self.entrypoint[0]) is not str
            or not self.entrypoint[0].startswith("/")
        ):
            raise ValueError("OCI evidence is invalid")
        return self


class ContainerBootstrapArtifactWorkerAttestationV4(_Model):
    """A worker's signed claim, never a locally recomputed source/build proof."""

    schema_version: Literal["rsd.container-bootstrap-artifact-worker-attestation.v4"]
    signer_key_id: str = Field(pattern=_IDENTIFIER)
    worker_identity_sha256: str = Field(pattern=_SHA256)
    run_id: str = Field(pattern=_IDENTIFIER)
    canonical_repository_identity_sha256: str = Field(pattern=_SHA256)
    git_object_format: Literal["sha1"]
    commit_oid: str = Field(pattern=_OID)
    tree_oid: str = Field(pattern=_OID)
    canonical_source_snapshot_sha256: str = Field(pattern=_SHA256)
    wrapper_subtree_path: str = Field(min_length=1, max_length=240)
    wrapper_tree_entries: tuple[ContainerBootstrapWrapperTreeEntryV4, ...] = Field(
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
    physical_builder_identity_sha256: str = Field(pattern=_SHA256)
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    component_role: Literal["infisical", "valkey"]
    static_role_profile_sha256: str = Field(pattern=_SHA256)
    profile_envelope_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
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
    oci: ContainerBootstrapOciEvidenceV4
    signature_base64: str = Field(min_length=4, max_length=128)

    @field_validator("wrapper_executable_path")
    @classmethod
    def canonical_executable_path(cls, value: str) -> str:
        return _safe_path(value)

    @field_validator("wrapper_subtree_path")
    @classmethod
    def safe_subtree(cls, value: str) -> str:
        return ContainerBootstrapWrapperTreeEntryV4.safe_relative_path(value)

    @field_validator("wrapper_tree_entries", mode="before")
    @classmethod
    def entries_tuple_only(cls, value: object) -> tuple[object, ...]:
        return _items(
            value, field="wrapper_tree_entries", max_items=_MAX_WRAPPER_TREE_ENTRIES
        )

    @model_validator(mode="after")
    def exact_attestation(self) -> Self:
        entries = self.wrapper_tree_entries
        expected_role = "valkey" if self.component.endswith("valkey") else "infisical"
        if (
            type(self.oci) is not ContainerBootstrapOciEvidenceV4
            or len(_b64(self.signature_base64)) != 64
            or self.component_role != expected_role
            or self.wrapper_executable_path != self.oci.wrapper_tar_entry.path
            or self.wrapper_executable_path != self.oci.entrypoint[0]
            or self.wrapper_artifact_sha256 != self.oci.wrapper_tar_entry.content_sha256
            or self.wrapper_artifact_byte_count != self.oci.wrapper_tar_entry.byte_count
            or len({entry.path for entry in entries}) != len(entries)
            or tuple(entry.path for entry in entries)
            != tuple(sorted(entry.path for entry in entries))
            or any(
                type(entry) is not ContainerBootstrapWrapperTreeEntryV4
                for entry in entries
            )
            or len(
                {
                    self.recipe_sha256,
                    self.toolchain_sha256,
                    self.lock_sha256,
                    self.vendor_sha256,
                    self.builder_recipe_identity_sha256,
                    self.physical_builder_identity_sha256,
                    self.worker_identity_sha256,
                }
            )
            != 7
        ):
            raise ValueError("worker attestation is invalid")
        _safe_path(self.wrapper_executable_path)
        return self


class ContainerBootstrapArtifactEvidenceClosureV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-artifact-evidence-closure.v4"]
    worker_attestations: tuple[
        ContainerBootstrapArtifactWorkerAttestationV4,
        ContainerBootstrapArtifactWorkerAttestationV4,
    ]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]

    @field_validator("worker_attestations", mode="before")
    @classmethod
    def exactly_two(cls, value: object) -> tuple[object, object]:
        result = _items(value, field="worker_attestations", max_items=2)
        if len(result) != 2:
            raise ValueError("closure must have two workers")
        return result


class ContainerBootstrapArtifactEvidenceAcceptanceV4(_Model):
    schema_version: Literal["rsd.container-bootstrap-artifact-evidence-acceptance.v4"]
    closure_sha256: str = Field(pattern=_SHA256)
    verification_context_sha256: str = Field(pattern=_SHA256)
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]


def container_bootstrap_artifact_worker_attestation_v4_canonical_json(
    attestation: ContainerBootstrapArtifactWorkerAttestationV4,
) -> bytes:
    try:
        return _canonical(
            _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV4)
        )
    except (TypeError, ValueError):
        _fail("binding")


def parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapArtifactWorkerAttestationV4:
    try:
        return _parse(payload, ContainerBootstrapArtifactWorkerAttestationV4)
    except ValueError:
        _fail("parse")


def parse_container_bootstrap_build_worker_trust_policy_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapBuildWorkerTrustPolicyV4:
    """Parse one canonical, externally supplied two-worker trust policy."""

    try:
        return _parse(payload, ContainerBootstrapBuildWorkerTrustPolicyV4)
    except ValueError:
        _fail("parse")


def container_bootstrap_artifact_worker_attestation_v4_message(
    attestation: ContainerBootstrapArtifactWorkerAttestationV4,
) -> bytes:
    try:
        return _DOMAIN_ATTESTATION + _canonical(
            _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV4),
            exclude={"signature_base64"},
        )
    except (TypeError, ValueError):
        _fail("binding")


def container_bootstrap_artifact_worker_attestation_v4_sha256(
    attestation: ContainerBootstrapArtifactWorkerAttestationV4,
) -> str:
    try:
        canonical = _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV4)
        return _hash(_DOMAIN_ATTESTATION_HASH, canonical)
    except (TypeError, ValueError):
        _fail("binding")


def container_bootstrap_artifact_evidence_closure_v4_canonical_json(
    closure: ContainerBootstrapArtifactEvidenceClosureV4,
) -> bytes:
    try:
        return _canonical(_strict(closure, ContainerBootstrapArtifactEvidenceClosureV4))
    except (TypeError, ValueError):
        _fail("binding")


def parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapArtifactEvidenceClosureV4:
    try:
        return _parse(payload, ContainerBootstrapArtifactEvidenceClosureV4)
    except ValueError:
        _fail("parse")


def _verify_worker(
    attestation: ContainerBootstrapArtifactWorkerAttestationV4,
    anchor: ContainerBootstrapBuildWorkerTrustAnchorV4,
) -> None:
    try:
        canonical = _strict(attestation, ContainerBootstrapArtifactWorkerAttestationV4)
        exact_anchor = _strict(anchor, ContainerBootstrapBuildWorkerTrustAnchorV4)
        if (
            canonical.signer_key_id != exact_anchor.key_id
            or canonical.worker_identity_sha256 != exact_anchor.worker_identity_sha256
        ):
            raise ValueError("anchor mismatch")
        Ed25519PublicKey.from_public_bytes(_b64(exact_anchor.public_key_base64)).verify(
            _b64(canonical.signature_base64),
            container_bootstrap_artifact_worker_attestation_v4_message(canonical),
        )
    except (
        InvalidSignature,
        TypeError,
        ValueError,
        ContainerBootstrapArtifactEvidenceV4Error,
    ):
        _fail("signature")


def validate_container_bootstrap_artifact_evidence_closure_v4(
    *,
    closure: ContainerBootstrapArtifactEvidenceClosureV4,
    worker_trust_policy: ContainerBootstrapBuildWorkerTrustPolicyV4,
    profile_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
    profile_trust_anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
) -> ContainerBootstrapArtifactEvidenceAcceptanceV4:
    """Validate two independent signed claims; this never authorizes any effect.

    The profile envelope is verified first.  Claims of repository and source
    state remain worker attestations, and no source or OCI material is read.
    """
    try:
        checked = _strict(closure, ContainerBootstrapArtifactEvidenceClosureV4)
        policy = _strict(
            worker_trust_policy, ContainerBootstrapBuildWorkerTrustPolicyV4
        )
        first_anchor, second_anchor = policy.worker_trust_anchors
        first_anchor = _strict(first_anchor, ContainerBootstrapBuildWorkerTrustAnchorV4)
        second_anchor = _strict(
            second_anchor, ContainerBootstrapBuildWorkerTrustAnchorV4
        )
        profile = verify_container_bootstrap_static_role_profile_envelope_v4(
            envelope=profile_envelope, profile_trust_anchor=profile_trust_anchor
        )
        canonical_profile_anchor = (
            strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(
                profile_trust_anchor
            )
        )
        envelope_hash = container_bootstrap_static_role_profile_envelope_v4_sha256(
            profile_envelope
        )
    except (TypeError, ValueError, ContainerBootstrapArtifactEvidenceV4Error):
        _fail("profile")
    if (
        len({policy.policy_id, first_anchor.key_id, second_anchor.key_id}) != 3
        or len(
            {
                policy.independence_domain_sha256,
                first_anchor.worker_identity_sha256,
                first_anchor.authority_identity_sha256,
                second_anchor.worker_identity_sha256,
                second_anchor.authority_identity_sha256,
            }
        )
        != 5
        or first_anchor.public_key_base64 == second_anchor.public_key_base64
    ):
        _fail("anchor")
    profile_roots = (
        canonical_profile_anchor,
        profile.ticket_trust_anchor,
        profile.replay_receipt_trust_anchor,
    )
    profile_root_fingerprints = {
        root.public_key_fingerprint_sha256 for root in profile_roots
    }
    if policy.policy_id in {root.key_id for root in profile_roots} or (
        {
            policy.independence_domain_sha256,
            first_anchor.worker_identity_sha256,
            first_anchor.authority_identity_sha256,
            second_anchor.worker_identity_sha256,
            second_anchor.authority_identity_sha256,
            first_anchor.public_key_fingerprint_sha256,
            second_anchor.public_key_fingerprint_sha256,
        }
        & profile_root_fingerprints
    ):
        _fail("anchor")
    for worker_anchor in (first_anchor, second_anchor):
        for profile_root in profile_roots:
            if (
                worker_anchor.key_id == profile_root.key_id
                or worker_anchor.public_key_fingerprint_sha256
                == profile_root.public_key_fingerprint_sha256
                or worker_anchor.public_key_base64 == profile_root.public_key_base64
            ):
                _fail("anchor")
    first, second = checked.worker_attestations
    _verify_worker(first, first_anchor)
    _verify_worker(second, second_anchor)
    source_fields = (
        "canonical_repository_identity_sha256",
        "git_object_format",
        "commit_oid",
        "tree_oid",
        "canonical_source_snapshot_sha256",
        "wrapper_subtree_path",
        "wrapper_tree_entries",
        "recipe_sha256",
        "toolchain_sha256",
        "lock_sha256",
        "vendor_sha256",
        "builder_recipe_identity_sha256",
        "component",
        "component_role",
        "static_role_profile_sha256",
        "profile_envelope_sha256",
        "static_delivery_projection_sha256",
        "static_launch_plan_sha256",
        "static_patch_preimage_sha256",
        "static_patch_policy_sha256",
        "wrapper_artifact_sha256",
        "wrapper_artifact_byte_count",
        "wrapper_executable_path",
        "oci",
    )
    if first.run_id == second.run_id or any(
        getattr(first, name) != getattr(second, name) for name in source_fields
    ):
        _fail("binding")
    if (
        first.physical_builder_identity_sha256
        == second.physical_builder_identity_sha256
    ):
        _fail("binding")
    if len(
        {
            policy.independence_domain_sha256,
            first_anchor.worker_identity_sha256,
            first_anchor.authority_identity_sha256,
            second_anchor.worker_identity_sha256,
            second_anchor.authority_identity_sha256,
            first_anchor.public_key_fingerprint_sha256,
            second_anchor.public_key_fingerprint_sha256,
            first.physical_builder_identity_sha256,
            second.physical_builder_identity_sha256,
        }
    ) != 9 or (
        {
            first.physical_builder_identity_sha256,
            second.physical_builder_identity_sha256,
        }
        & profile_root_fingerprints
    ):
        _fail("binding")
    if (
        first.component != profile.component
        or first.component_role != profile.component_role
        or first.static_role_profile_sha256 != profile.profile_sha256
        or first.profile_envelope_sha256 != envelope_hash
        or first.static_delivery_projection_sha256
        != profile.static_delivery_projection_sha256
        or first.static_launch_plan_sha256 != profile.static_launch_plan_sha256
        or first.static_patch_preimage_sha256 != profile.static_patch_preimage_sha256
        or first.static_patch_policy_sha256 != profile.static_patch_policy_sha256
        or first.wrapper_executable_path != profile.wrapper_executable_path
        or first.oci.base_image_policy_sha256
        != profile.static_launch_plan.base_image_policy_sha256
        or first.oci.base_resolution_attestation_sha256
        != profile.static_launch_plan.base_resolution_attestation_sha256
        or first.oci.base_registry_index_digest_sha256
        != profile.static_launch_plan.base_registry_index_digest_sha256
        or first.oci.base_linux_amd64_manifest_digest_sha256
        != profile.static_launch_plan.base_linux_amd64_manifest_digest_sha256
        or first.oci.base_config_digest_sha256
        != profile.static_launch_plan.base_config_digest_sha256
        or first.oci.entrypoint
        != profile.static_launch_plan.wrapper_argv_prefix
        + profile.static_launch_plan.base_entrypoint
        or first.oci.cmd != profile.static_launch_plan.base_command
        or first.canonical_source_snapshot_sha256
        != profile.static_patch_preimage.wrapper_source_tree_sha256
    ):
        _fail("binding")
    return ContainerBootstrapArtifactEvidenceAcceptanceV4(
        schema_version="rsd.container-bootstrap-artifact-evidence-acceptance.v4",
        closure_sha256=_hash(_DOMAIN_CLOSURE, checked),
        verification_context_sha256=hashlib.sha256(
            _DOMAIN_CONTEXT
            + canonical_profile_anchor.public_key_fingerprint_sha256.encode("ascii")
            + _canonical(policy)
        ).hexdigest(),
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
