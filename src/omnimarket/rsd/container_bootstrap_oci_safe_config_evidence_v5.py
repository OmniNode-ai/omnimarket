"""Pure, redacted Phase-A V5 OCI safe-config evidence.

This module validates a bounded statement about already-collected raw OCI
index, manifest, wrapper archive, and config material.  It never contacts a
registry, filesystem, container engine, or environment.  The OCI config itself
is transient verifier input: the durable evidence carries only the exact
value-redacted V5 config commitment claim.

Successful verification is deliberately non-authorizing and authenticates
nothing.  It does not prove a role, static profile, source snapshot, worker
identity, build authenticity, or permission to create, materialize, or attach
a container.  A later signed V5 worker attestation must bind those omitted
facts, including authenticated source/profile context.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Final, Literal, NoReturn, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from omnimarket.rsd.oci_config_commitment import (
    OciConfigCommitmentError,
    PhaseAV5ExpandedOciConfigCommitmentClaimV1,
    verify_phase_a_v5_expanded_oci_config_claim_v1,
)
from omnimarket.rsd.oci_repository import (
    oci_repository_reference_v1,
    validate_oci_repository_reference_v1,
    validate_oci_repository_v1,
)

_SHA256: Final = r"^[0-9a-f]{64}$"
_PATH: Final = r"^/[A-Za-z0-9._/-]{1,240}$"
_ARGV: Final = r"^[A-Za-z0-9._/:=@+,%=-]{1,256}$"
_OCI_INDEX_MEDIA_TYPE: Final = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST_MEDIA_TYPE: Final = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG_MEDIA_TYPE: Final = "application/vnd.oci.image.config.v1+json"
_OCI_LAYER_MEDIA_TYPE: Final = "application/vnd.oci.image.layer.v1.tar+gzip"
_MAX_LAYERS: Final = 32
_MAX_INDEX_JSON_BYTES: Final = 8_192
_MAX_MANIFEST_JSON_BYTES: Final = 8_192
_MAX_INDEX_BASE64_CHARS: Final = ((_MAX_INDEX_JSON_BYTES + 2) // 3) * 4
_MAX_MANIFEST_BASE64_CHARS: Final = ((_MAX_MANIFEST_JSON_BYTES + 2) // 3) * 4
_MAX_LAYER_BYTES: Final = 1_073_741_824
_MAX_WRAPPER_BYTES: Final = 67_108_864
_MAX_ENTRYPOINT_ITEMS: Final = 128
_MAX_ENTRYPOINT_BYTES: Final = 32_768
_MAX_CMD_ITEMS: Final = 64
_MAX_CMD_BYTES: Final = 16_384
_MAX_ARCHIVE_ENTRIES: Final = 65_536
_MAX_EVIDENCE_CANONICAL_BYTES: Final = 131_072
_MAX_DEPTH: Final = 24
_MAX_NODES: Final = 4_096
_EVIDENCE_DOMAIN: Final = (
    b"omninode-rsd.container-bootstrap-oci-safe-config-evidence.sha256.v5\x00"
)


class ContainerBootstrapOciSafeConfigEvidenceV5Error(ValueError):
    """Fixed, value-redacted failure for the V5 safe-config evidence domain."""

    def __init__(self) -> None:
        super().__init__(
            "container bootstrap V5 OCI safe-config evidence validation failed"
        )


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail() -> NoReturn:
    raise ContainerBootstrapOciSafeConfigEvidenceV5Error()


def _canonical_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("canonical JSON is invalid") from None
    if len(payload) > _MAX_EVIDENCE_CANONICAL_BYTES:
        raise ValueError("canonical JSON exceeds bound")
    return payload


def _canonical_model(model: BaseModel) -> bytes:
    return _canonical_json(model.model_dump(mode="json", warnings="error"))


def _no_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("JSON is invalid")
        result[key] = value
    return result


def _reject_float(_value: str) -> NoReturn:
    raise ValueError("JSON float is invalid")


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("JSON constant is invalid")


def _preflight(payload: bytes, *, max_bytes: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= max_bytes:
        raise ValueError("JSON is invalid")
    depth = 0
    quoted = False
    escaped = False
    for byte in payload:
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


def _validate_tree_bounds(
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
            _validate_tree_bounds(child, depth=depth + 1, nodes=nodes)
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_tree_bounds(child, depth=depth + 1, nodes=nodes)


def _arrays_to_tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_arrays_to_tuples(item) for item in cast(list[object], value))
    if type(value) is dict:
        return {
            key: _arrays_to_tuples(item)
            for key, item in cast(dict[str, object], value).items()
        }
    return value


def _same_shape(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return isinstance(right, BaseModel) and all(
            _same_shape(getattr(left, key), getattr(right, key))
            for key in left.__class__.model_fields
        )
    if type(left) is tuple:
        right_tuple = cast(tuple[object, ...], right)
        return len(left) == len(right_tuple) and all(
            _same_shape(item, candidate)
            for item, candidate in zip(left, right_tuple, strict=True)
        )
    return left == right


def _exact_json_equal(left: object, right: object) -> bool:
    """Compare JSON trees without Python's bool/integer equality aliases."""

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        left_dict = cast(dict[str, object], left)
        right_dict = cast(dict[str, object], right)
        if len(left_dict) != len(right_dict):
            return False
        for key, child in left_dict.items():
            if type(key) is not str or key not in right_dict:
                return False
            if not _exact_json_equal(child, right_dict[key]):
                return False
        return True
    if type(left) is list:
        left_list = cast(list[object], left)
        right_list = cast(list[object], right)
        return len(left_list) == len(right_list) and all(
            _exact_json_equal(item, candidate)
            for item, candidate in zip(left_list, right_list, strict=True)
        )
    return left == right


def _items(value: object, *, maximum: int, minimum: int = 1) -> tuple[object, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        raise ValueError("sequence is invalid")
    return cast(tuple[object, ...], value)


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


def _b64(value: str, *, maximum_bytes: int) -> bytes:
    if type(value) is not str:
        raise ValueError("base64 is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("base64 is invalid") from None
    if (
        len(decoded) > maximum_bytes
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise ValueError("base64 is invalid")
    return decoded


def _canonical_json_object(payload: bytes, *, maximum_bytes: int) -> dict[str, object]:
    _preflight(payload, max_bytes=maximum_bytes)
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise ValueError("OCI JSON is invalid") from None
    if type(decoded) is not dict:
        raise ValueError("OCI JSON is invalid")
    document = cast(dict[str, object], decoded)
    _validate_tree_bounds(document)
    try:
        rendered = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("OCI JSON is invalid") from None
    if rendered != payload:
        raise ValueError("OCI JSON is invalid")
    return document


def _strict_claim(value: object) -> PhaseAV5ExpandedOciConfigCommitmentClaimV1:
    if type(value) is not PhaseAV5ExpandedOciConfigCommitmentClaimV1:
        raise ValueError("claim is invalid")
    try:
        payload = json.dumps(
            value.model_dump(mode="json", warnings="error"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        parsed = PhaseAV5ExpandedOciConfigCommitmentClaimV1.model_validate_json(
            payload, strict=True
        )
    except (TypeError, ValidationError, ValueError):
        raise ValueError("claim is invalid") from None
    if value != parsed or not _same_shape(value, parsed):
        raise ValueError("claim is invalid")
    return parsed


class ContainerBootstrapOciSafeConfigLayerDescriptorV5(_Model):
    """One exact gzip layer descriptor and its uncompressed diff ID."""

    schema_version: Literal[
        "rsd.container-bootstrap-oci-safe-config-layer-descriptor.v5"
    ]
    media_type: Literal["application/vnd.oci.image.layer.v1.tar+gzip"]
    digest_sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=1, le=_MAX_LAYER_BYTES)
    diff_id_sha256: str = Field(pattern=_SHA256)

    @field_validator("byte_count", mode="before")
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("layer byte count is invalid")
        return value


class ContainerBootstrapOciSafeConfigWrapperEntryV5(_Model):
    """One root-owned, executable regular wrapper file in the final layer."""

    schema_version: Literal["rsd.container-bootstrap-oci-safe-config-wrapper-entry.v5"]
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
    byte_count: int = Field(ge=1, le=_MAX_WRAPPER_BYTES)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return _safe_path(value)

    @field_validator("uid", "gid", "link_count", "byte_count", mode="before")
    @classmethod
    def exact_integer_literals(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("wrapper integer is invalid")
        return value

    @field_validator("symlink", "hardlink", "setuid", "setgid", "sticky", mode="before")
    @classmethod
    def exact_boolean_literals(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("wrapper boolean is invalid")
        return value


class ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5(_Model):
    """An exhaustive, signed-later archive claim; collection is out of scope."""

    schema_version: Literal[
        "rsd.container-bootstrap-oci-safe-config-wrapper-archive-inspection.v5"
    ]
    archive_entry_count: int = Field(ge=1, le=_MAX_ARCHIVE_ENTRIES)
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
    def canonical_path(cls, value: str) -> str:
        return _safe_path(value)

    @field_validator(
        "archive_entry_count", "wrapper_matching_entry_count", mode="before"
    )
    @classmethod
    def exact_integer_literal(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("archive integer is invalid")
        return value

    @field_validator(
        "duplicate_names_absent",
        "pax_headers_absent",
        "gnu_longname_or_link_absent",
        "traversal_paths_absent",
        "absolute_paths_absent",
        "whiteout_entries_absent",
        "privilege_bits_absent",
        "symlink_entries_absent",
        "hardlink_entries_absent",
        "device_fifo_socket_entries_absent",
        "sparse_entries_absent",
        "nonregular_entries_absent",
        "trailing_conflicting_wrapper_entries_absent",
        mode="before",
    )
    @classmethod
    def exact_boolean_literals(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("archive boolean is invalid")
        return value


class ContainerBootstrapOciSafeConfigEvidenceV5(_Model):
    """Bounded, value-redacted and non-authorizing Phase-A V5 OCI evidence."""

    schema_version: Literal["rsd.container-bootstrap-oci-safe-config-evidence.v5"]
    derived_repository: str = Field(max_length=240)
    derived_reference: str = Field(max_length=312)
    index_digest_sha256: str = Field(pattern=_SHA256)
    index_canonical_json_sha256: str = Field(pattern=_SHA256)
    index_canonical_json_byte_count: int = Field(ge=2, le=_MAX_INDEX_JSON_BYTES)
    index_canonical_json_utf8_base64: str = Field(
        min_length=4, max_length=_MAX_INDEX_BASE64_CHARS
    )
    selected_manifest_descriptor_digest_sha256: str = Field(pattern=_SHA256)
    linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    manifest_canonical_json_sha256: str = Field(pattern=_SHA256)
    manifest_canonical_json_byte_count: int = Field(ge=2, le=_MAX_MANIFEST_JSON_BYTES)
    manifest_canonical_json_utf8_base64: str = Field(
        min_length=4, max_length=_MAX_MANIFEST_BASE64_CHARS
    )
    platform_os: Literal["linux"]
    platform_architecture: Literal["amd64"]
    ordered_layers: tuple[ContainerBootstrapOciSafeConfigLayerDescriptorV5, ...] = (
        Field(min_length=1, max_length=_MAX_LAYERS)
    )
    config_rootfs_diff_ids_sha256: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_LAYERS
    )
    wrapper_layer_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_layer_ordinal: int = Field(ge=0, le=_MAX_LAYERS - 1)
    wrapper_tar_entry: ContainerBootstrapOciSafeConfigWrapperEntryV5
    wrapper_archive_inspection: (
        ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5
    )
    entrypoint: tuple[str, ...] = Field(min_length=1, max_length=_MAX_ENTRYPOINT_ITEMS)
    cmd: tuple[str, ...] = Field(max_length=_MAX_CMD_ITEMS)
    expanded_oci_config_claim: PhaseAV5ExpandedOciConfigCommitmentClaimV1
    non_authorizing: Literal[True]
    evidence_effect_allowed: Literal[False]
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]

    @field_validator(
        "ordered_layers",
        "config_rootfs_diff_ids_sha256",
        "entrypoint",
        "cmd",
        mode="before",
    )
    @classmethod
    def tuple_only(cls, value: object, info: ValidationInfo) -> tuple[object, ...]:
        minimum = 0 if info.field_name == "cmd" else 1
        maximum = {
            "ordered_layers": _MAX_LAYERS,
            "config_rootfs_diff_ids_sha256": _MAX_LAYERS,
            "entrypoint": _MAX_ENTRYPOINT_ITEMS,
            "cmd": _MAX_CMD_ITEMS,
        }.get(str(info.field_name))
        if maximum is None:
            raise ValueError("OCI sequence is invalid")
        return _items(value, maximum=maximum, minimum=minimum)

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
        maximum = (
            _MAX_ENTRYPOINT_BYTES if info.field_name == "entrypoint" else _MAX_CMD_BYTES
        )
        if (
            any(
                type(item) is not str or re.fullmatch(_ARGV, item) is None
                for item in value
            )
            or sum(len(item.encode("ascii")) for item in value) > maximum
        ):
            raise ValueError("OCI argv is invalid")
        return value

    @field_validator(
        "index_canonical_json_byte_count",
        "manifest_canonical_json_byte_count",
        "wrapper_layer_ordinal",
        mode="before",
    )
    @classmethod
    def exact_integer_fields(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("OCI integer is invalid")
        return value

    @field_validator(
        "non_authorizing",
        "evidence_effect_allowed",
        "build_allowed",
        "materialization_allowed",
        "attach_allowed",
        mode="before",
    )
    @classmethod
    def exact_boolean_literals(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("effect boolean is invalid")
        return value

    @model_validator(mode="after")
    def exact_oci_graph(self) -> Self:
        try:
            index_bytes = _b64(
                self.index_canonical_json_utf8_base64,
                maximum_bytes=_MAX_INDEX_JSON_BYTES,
            )
            manifest_bytes = _b64(
                self.manifest_canonical_json_utf8_base64,
                maximum_bytes=_MAX_MANIFEST_JSON_BYTES,
            )
            index = _canonical_json_object(
                index_bytes, maximum_bytes=_MAX_INDEX_JSON_BYTES
            )
            manifest = _canonical_json_object(
                manifest_bytes, maximum_bytes=_MAX_MANIFEST_JSON_BYTES
            )
            claim = _strict_claim(self.expanded_oci_config_claim)
        except ValueError:
            raise ValueError("OCI safe-config evidence is invalid") from None
        layers = tuple(self.ordered_layers)
        layer_digests = tuple(layer.digest_sha256 for layer in layers)
        diff_ids = tuple(layer.diff_id_sha256 for layer in layers)
        expected_reference = oci_repository_reference_v1(
            self.derived_repository, self.linux_amd64_manifest_digest_sha256
        )
        selected = {
            "digest": f"sha256:{self.linux_amd64_manifest_digest_sha256}",
            "mediaType": _OCI_MANIFEST_MEDIA_TYPE,
            "platform": {"architecture": "amd64", "os": "linux"},
            "size": self.manifest_canonical_json_byte_count,
        }
        manifest_layers = [
            {
                "digest": f"sha256:{layer.digest_sha256}",
                "mediaType": _OCI_LAYER_MEDIA_TYPE,
                "size": layer.byte_count,
            }
            for layer in layers
        ]
        expected_index = {
            "manifests": [selected],
            "mediaType": _OCI_INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
        expected_manifest = {
            "config": {
                "digest": claim.oci_config_descriptor_digest,
                "mediaType": claim.oci_config_descriptor_media_type,
                "size": claim.oci_config_descriptor_size,
            },
            "layers": manifest_layers,
            "mediaType": _OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        }
        if (
            type(self.wrapper_tar_entry)
            is not ContainerBootstrapOciSafeConfigWrapperEntryV5
            or type(self.wrapper_archive_inspection)
            is not ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5
            or type(self.expanded_oci_config_claim)
            is not PhaseAV5ExpandedOciConfigCommitmentClaimV1
            or len(index_bytes) != self.index_canonical_json_byte_count
            or len(manifest_bytes) != self.manifest_canonical_json_byte_count
            or hashlib.sha256(index_bytes).hexdigest()
            != self.index_canonical_json_sha256
            or hashlib.sha256(manifest_bytes).hexdigest()
            != self.manifest_canonical_json_sha256
            or self.index_digest_sha256 != self.index_canonical_json_sha256
            or self.linux_amd64_manifest_digest_sha256
            != self.manifest_canonical_json_sha256
            or self.selected_manifest_descriptor_digest_sha256
            != self.linux_amd64_manifest_digest_sha256
            or not _exact_json_equal(index, expected_index)
            or not _exact_json_equal(manifest, expected_manifest)
            or self.derived_reference != expected_reference
            or len(set(layer_digests)) != len(layer_digests)
            or len(set(diff_ids)) != len(diff_ids)
            or self.config_rootfs_diff_ids_sha256 != diff_ids
            or any(
                type(diff_id) is not str or re.fullmatch(_SHA256, diff_id) is None
                for diff_id in self.config_rootfs_diff_ids_sha256
            )
            or self.wrapper_layer_ordinal != len(layers) - 1
            or self.wrapper_layer_digest_sha256 != layers[-1].digest_sha256
            or self.wrapper_archive_inspection.inspected_layer_digest_sha256
            != self.wrapper_layer_digest_sha256
            or self.wrapper_archive_inspection.wrapper_path
            != self.wrapper_tar_entry.path
            or self.entrypoint[0] != self.wrapper_tar_entry.path
            or not self.entrypoint[0].startswith("/")
            or not self.non_authorizing
            or self.evidence_effect_allowed
            or self.build_allowed
            or self.materialization_allowed
            or self.attach_allowed
        ):
            raise ValueError("OCI safe-config evidence is invalid")
        return self


def _strict_evidence(
    evidence: object,
) -> ContainerBootstrapOciSafeConfigEvidenceV5:
    if type(evidence) is not ContainerBootstrapOciSafeConfigEvidenceV5:
        raise ValueError("evidence type is invalid")
    payload = _canonical_model(evidence)
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
        canonical = ContainerBootstrapOciSafeConfigEvidenceV5.model_validate(
            _arrays_to_tuples(decoded), strict=True
        )
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError, RecursionError):
        raise ValueError("evidence is invalid") from None
    if _canonical_model(canonical) != payload or not _same_shape(evidence, canonical):
        raise ValueError("evidence is invalid")
    return canonical


def strict_canonical_container_bootstrap_oci_safe_config_evidence_v5(
    evidence: ContainerBootstrapOciSafeConfigEvidenceV5,
) -> ContainerBootstrapOciSafeConfigEvidenceV5:
    """Reparse one exact V5 evidence value without authorizing any effect."""

    try:
        return _strict_evidence(evidence)
    except (TypeError, ValueError):
        _fail()


def container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
    evidence: ContainerBootstrapOciSafeConfigEvidenceV5,
) -> bytes:
    """Return exact bounded canonical V5 evidence JSON, never raw config bytes."""

    try:
        return _canonical_model(_strict_evidence(evidence))
    except (TypeError, ValueError):
        _fail()


def parse_container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
    payload: bytes,
) -> ContainerBootstrapOciSafeConfigEvidenceV5:
    """Parse only one exact bounded canonical V5 evidence JSON spelling."""

    try:
        _preflight(payload, max_bytes=_MAX_EVIDENCE_CANONICAL_BYTES)
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
        if type(decoded) is not dict:
            raise ValueError("evidence JSON is invalid")
        _validate_tree_bounds(decoded)
        evidence = ContainerBootstrapOciSafeConfigEvidenceV5.model_validate(
            _arrays_to_tuples(decoded), strict=True
        )
        if _canonical_model(evidence) != payload:
            raise ValueError("evidence JSON is invalid")
        return _strict_evidence(evidence)
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError, RecursionError):
        _fail()


def container_bootstrap_oci_safe_config_evidence_v5_sha256(
    evidence: ContainerBootstrapOciSafeConfigEvidenceV5,
) -> str:
    """Hash exact V5 evidence under its dedicated non-authorizing domain."""

    try:
        canonical = _canonical_model(_strict_evidence(evidence))
        return hashlib.sha256(_EVIDENCE_DOMAIN + canonical).hexdigest()
    except (TypeError, ValueError):
        _fail()


def _safe_config_projection(
    raw_config_bytes: bytes,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Read only argv and diff IDs after the redacted claim verifier accepted bytes."""

    try:
        config = _canonical_json_object(raw_config_bytes, maximum_bytes=65_536)
        if config.get("architecture") != "amd64" or config.get("os") != "linux":
            raise ValueError("config platform is invalid")
        image_config = config.get("config")
        rootfs = config.get("rootfs")
        if type(image_config) is not dict or type(rootfs) is not dict:
            raise ValueError("config shape is invalid")
        entrypoint = image_config.get("Entrypoint")
        cmd = image_config.get("Cmd")
        diff_ids = rootfs.get("diff_ids")
        if (
            type(entrypoint) is not list
            or type(cmd) is not list
            or type(diff_ids) is not list
        ):
            raise ValueError("config shape is invalid")
        entrypoint_tuple = tuple(cast(str, item) for item in entrypoint)
        cmd_tuple = tuple(cast(str, item) for item in cmd)
        diff_ids_tuple = tuple(cast(str, item) for item in diff_ids)
        if (
            any(type(item) is not str for item in entrypoint_tuple)
            or any(type(item) is not str for item in cmd_tuple)
            or any(
                type(item) is not str
                or not item.startswith("sha256:")
                or re.fullmatch(_SHA256, item.removeprefix("sha256:")) is None
                for item in diff_ids_tuple
            )
        ):
            raise ValueError("config projection is invalid")
        return (
            entrypoint_tuple,
            cmd_tuple,
            tuple(item.removeprefix("sha256:") for item in diff_ids_tuple),
        )
    except (TypeError, ValueError):
        raise ValueError("config projection is invalid") from None


def verify_container_bootstrap_oci_safe_config_evidence_v5_internal_consistency(
    evidence: ContainerBootstrapOciSafeConfigEvidenceV5,
    raw_config_bytes: bytes,
) -> ContainerBootstrapOciSafeConfigEvidenceV5:
    """Verify raw V5 evidence internal consistency; this authenticates nothing.

    This verifier is non-authorizing and does not authenticate a worker, source
    snapshot, role, profile, build, OCI collection process, repository, or
    reference.  The evidence's canonical repository/reference relation is
    checked only as a self-contained raw-image consistency property.  A later
    signed V5 worker attestation must compare it with authenticated context.
    Raw config bytes are transient and are never returned or included in
    failures.
    """

    try:
        checked = _strict_evidence(evidence)
        claim = checked.expanded_oci_config_claim
        verify_phase_a_v5_expanded_oci_config_claim_v1(
            raw_config_bytes,
            claim.oci_config_descriptor_media_type,
            claim.oci_config_descriptor_digest,
            claim.oci_config_descriptor_size,
            claim,
        )
        entrypoint, cmd, diff_ids = _safe_config_projection(raw_config_bytes)
        if (
            entrypoint != checked.entrypoint
            or cmd != checked.cmd
            or diff_ids != checked.config_rootfs_diff_ids_sha256
            or checked.entrypoint[0] != checked.wrapper_tar_entry.path
            or not checked.non_authorizing
            or checked.evidence_effect_allowed
            or checked.build_allowed
            or checked.materialization_allowed
            or checked.attach_allowed
        ):
            raise ValueError("evidence binding is invalid")
        return checked
    except (
        OciConfigCommitmentError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        _fail()
