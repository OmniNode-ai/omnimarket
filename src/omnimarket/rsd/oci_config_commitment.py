"""Pure Phase-A V5 OCI-config commitment claims.

The caller supplies canonical OCI config bytes plus the exact config descriptor
tuple extracted from a separately verified OCI manifest: media type, digest,
and size.  This module neither fetches nor inspects an image.  It proves only
content/tuple consistency and returns a value-redacted *claim* shape for a
later signed Phase-A V5 expanded-config worker attestation.  It does not prove
manifest placement or linkage; the outer Phase-A V5 verifier must enforce that
descriptor-to-manifest relation.  Deriving or verifying the claim is never
signature verification, evidence of a build, or authorization.

The V5 expanded profile intentionally requires ``Cmd``, ``Entrypoint``,
``User``, ``WorkingDir``, and ``Env``.  The current V4 two-key OCI config is
therefore rejected by design rather than silently broadened here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Final, Literal, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field

_MAX_CONFIG_JSON_BYTES: Final = 65_536
_MAX_DEPTH: Final = 24
_MAX_NODES: Final = 4_096
_MAX_LAYERS: Final = 32
_MAX_ENTRYPOINT_ITEMS: Final = 128
_MAX_ENTRYPOINT_BYTES: Final = 32_768
_MAX_CMD_ITEMS: Final = 64
_MAX_CMD_BYTES: Final = 16_384
_MAX_ENV_ITEMS: Final = 32
_MAX_ENV_RENDERED_BYTES: Final = 16_384
_MAX_ENV_VALUE_BYTES: Final = 1_024
_MAX_WORKING_DIR_BYTES: Final = 240
_MAX_WORKING_DIR_SEGMENT_BYTES: Final = 64
_MAX_UID_GID: Final = 2_147_483_647
_OCI_CONFIG_DESCRIPTOR_MEDIA_TYPE: Final[
    Literal["application/vnd.oci.image.config.v1+json"]
] = "application/vnd.oci.image.config.v1+json"

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_OCI_DESCRIPTOR_DIGEST_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARGV_RE: Final = re.compile(r"^[A-Za-z0-9._/:=@+,%=-]{1,256}$")
_USER_RE: Final = re.compile(r"^([1-9][0-9]{0,9}):([1-9][0-9]{0,9})$")
_WORKING_DIR_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

_RESERVED_DELIVERY_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ENCRYPTION_KEY",
        "AUTH_SECRET",
        "DB_CONNECTION_URI",
        "REDIS_URL",
        "REQUIREPASS",
    }
)

_USER_DOMAIN: Final = b"omninode-rsd.oci-config-commitment.user.sha256.v1\x00"
_WORKING_DIR_DOMAIN: Final = (
    b"omninode-rsd.oci-config-commitment.working-dir.sha256.v1\x00"
)
_ENV_SEQUENCE_DOMAIN: Final = (
    b"omninode-rsd.oci-config-commitment.env-sequence.sha256.v1\x00"
)
_ENV_NAMES_DOMAIN: Final = b"omninode-rsd.oci-config-commitment.env-names.sha256.v1\x00"
_RESERVED_POLICY_DOMAIN: Final = (
    b"omninode-rsd.oci-config-commitment.reserved-delivery-env-policy.sha256.v1\x00"
)


class OciConfigCommitmentError(ValueError):
    """Fixed, redacted error for an invalid OCI configuration commitment."""

    def __init__(self) -> None:
        super().__init__("OCI config commitment is invalid")


class _ClaimModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


class PhaseAV5ExpandedOciConfigCommitmentClaimV1(_ClaimModel):
    """Unsigned, non-authorizing claim to be carried by a V5 worker attestation."""

    schema_version: Literal["rsd.phase-a-v5-expanded-oci-config-commitment-claim.v1"]
    signed_worker_attestation_scope: Literal["phase-a-v5-expanded-oci-config-profile"]
    non_authorizing: Literal[True]
    oci_config_descriptor_media_type: Literal[
        "application/vnd.oci.image.config.v1+json"
    ]
    oci_config_descriptor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    oci_config_descriptor_size: int = Field(ge=2, le=_MAX_CONFIG_JSON_BYTES)
    runtime_uid: int = Field(ge=1, le=_MAX_UID_GID)
    runtime_gid: int = Field(ge=1, le=_MAX_UID_GID)
    user_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_byte_count: int = Field(ge=3, le=21)
    working_dir_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_dir_byte_count: int = Field(ge=1, le=_MAX_WORKING_DIR_BYTES)
    environment_sequence_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_names_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_entry_count: int = Field(ge=0, le=_MAX_ENV_ITEMS)
    environment_rendered_byte_count: int = Field(ge=0, le=_MAX_ENV_RENDERED_BYTES)
    reserved_delivery_env_policy_commitment_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    reserved_delivery_env_names_absent: Literal[True]


_CLAIM_STRING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "signed_worker_attestation_scope",
        "oci_config_descriptor_media_type",
        "oci_config_descriptor_digest",
        "user_commitment_sha256",
        "working_dir_commitment_sha256",
        "environment_sequence_commitment_sha256",
        "environment_names_commitment_sha256",
        "reserved_delivery_env_policy_commitment_sha256",
    }
)
_CLAIM_INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "oci_config_descriptor_size",
        "runtime_uid",
        "runtime_gid",
        "user_byte_count",
        "working_dir_byte_count",
        "environment_entry_count",
        "environment_rendered_byte_count",
    }
)
_CLAIM_BOOLEAN_FIELDS: Final[frozenset[str]] = frozenset(
    {"non_authorizing", "reserved_delivery_env_names_absent"}
)
_CLAIM_FIELD_NAMES: Final[frozenset[str]] = (
    _CLAIM_STRING_FIELDS | _CLAIM_INTEGER_FIELDS | _CLAIM_BOOLEAN_FIELDS
)


def _fail() -> NoReturn:
    raise OciConfigCommitmentError()


def _no_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _fail()
        result[key] = value
    return result


def _reject_float(_value: str) -> NoReturn:
    _fail()


def _reject_constant(_value: str) -> NoReturn:
    _fail()


def _preflight(payload: bytes) -> None:
    """Reject malformed or too-deep JSON before the decoder allocates a tree."""

    if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_CONFIG_JSON_BYTES:
        _fail()
    quoted = escaped = False
    depth = 0
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            elif byte < 32:
                _fail()
            continue
        if byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            if depth > _MAX_DEPTH:
                _fail()
        elif byte in (125, 93):
            depth -= 1
            if depth < 0:
                _fail()
    if quoted or escaped or depth:
        _fail()


def _validate_tree_bounds(
    value: object, *, depth: int = 1, nodes: list[int] | None = None
) -> None:
    if nodes is None:
        nodes = [0]
    if depth > _MAX_DEPTH:
        _fail()
    nodes[0] += 1
    if nodes[0] > _MAX_NODES:
        _fail()
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str:
                _fail()
            nodes[0] += 1
            if nodes[0] > _MAX_NODES:
                _fail()
            _validate_tree_bounds(child, depth=depth + 1, nodes=nodes)
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_tree_bounds(child, depth=depth + 1, nodes=nodes)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError):
        _fail()


def _parse_canonical_json_object(payload: bytes) -> dict[str, object]:
    _preflight(payload)
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        _fail()
    if type(decoded) is not dict:
        _fail()
    result = cast(dict[str, object], decoded)
    _validate_tree_bounds(result)
    if _canonical_json(result) != payload:
        _fail()
    return result


def _ascii(value: object) -> str:
    if type(value) is not str or not value.isascii():
        _fail()
    return value


def _bounded_argv(value: object, *, entrypoint: bool) -> tuple[str, ...]:
    if type(value) is not list:
        _fail()
    raw = cast(list[object], value)
    maximum_items = _MAX_ENTRYPOINT_ITEMS if entrypoint else _MAX_CMD_ITEMS
    if (entrypoint and not raw) or len(raw) > maximum_items:
        _fail()
    result = tuple(_ascii(item) for item in raw)
    if (
        any(_ARGV_RE.fullmatch(item) is None for item in result)
        or sum(len(item.encode("ascii")) for item in result)
        > (_MAX_ENTRYPOINT_BYTES if entrypoint else _MAX_CMD_BYTES)
        or (entrypoint and not result[0].startswith("/"))
    ):
        _fail()
    return result


def _validate_rootfs(value: object) -> None:
    if type(value) is not dict:
        _fail()
    rootfs = cast(dict[str, object], value)
    if set(rootfs) != {"diff_ids", "type"} or rootfs["type"] != "layers":
        _fail()
    diff_ids = rootfs["diff_ids"]
    if type(diff_ids) is not list or not 1 <= len(diff_ids) <= _MAX_LAYERS:
        _fail()
    rendered = tuple(_ascii(item) for item in cast(list[object], diff_ids))
    if len(set(rendered)) != len(rendered) or any(
        not item.startswith("sha256:")
        or _SHA256_RE.fullmatch(item.removeprefix("sha256:")) is None
        for item in rendered
    ):
        _fail()


def _validate_user(value: object) -> tuple[str, int, int]:
    raw = _ascii(value)
    match = _USER_RE.fullmatch(raw)
    if match is None:
        _fail()
    uid, gid = int(match.group(1)), int(match.group(2))
    if uid > _MAX_UID_GID or gid > _MAX_UID_GID:
        _fail()
    return raw, uid, gid


def _validate_working_dir(value: object) -> str:
    raw = _ascii(value)
    if len(raw.encode("ascii")) > _MAX_WORKING_DIR_BYTES or not raw.startswith("/"):
        _fail()
    if raw == "/":
        return raw
    if raw.endswith("/") or "//" in raw or "\\" in raw or "%" in raw:
        _fail()
    parts = raw.split("/")
    if (
        not parts
        or parts[0] != ""
        or any(_WORKING_DIR_SEGMENT_RE.fullmatch(part) is None for part in parts[1:])
    ):
        _fail()
    return raw


def _validate_env(value: object) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    if type(value) is not list:
        _fail()
    raw = cast(list[object], value)
    if len(raw) > _MAX_ENV_ITEMS:
        _fail()
    entries = tuple(_ascii(item) for item in raw)
    names: list[str] = []
    for entry in entries:
        name, separator, env_value = entry.partition("=")
        if (
            not separator
            or _ENV_NAME_RE.fullmatch(name) is None
            or len(env_value.encode("ascii")) > _MAX_ENV_VALUE_BYTES
            or any(not 32 <= ord(char) <= 126 for char in env_value)
            or name.upper() in _RESERVED_DELIVERY_ENV_NAMES
        ):
            _fail()
        names.append(name)
    names_tuple = tuple(names)
    if names_tuple != tuple(sorted(names_tuple)) or len(set(names_tuple)) != len(
        names_tuple
    ):
        _fail()
    byte_count = sum(len(entry.encode("ascii")) for entry in entries)
    if byte_count > _MAX_ENV_RENDERED_BYTES:
        _fail()
    return entries, names_tuple, byte_count


def _bound_hash(domain: bytes, config_digest: str, value: bytes) -> str:
    """Domain-separate a commitment and bind it to the full OCI config digest."""

    return hashlib.sha256(domain + config_digest.encode("ascii") + value).hexdigest()


def _sequence_bytes(values: tuple[str, ...]) -> bytes:
    return _canonical_json(list(values))


def _validate_expected_descriptor(
    raw_config_bytes: bytes,
    expected_config_descriptor_media_type: str,
    expected_config_descriptor_digest: str,
    expected_config_descriptor_size: int,
) -> str:
    if (
        type(expected_config_descriptor_media_type) is not str
        or expected_config_descriptor_media_type != _OCI_CONFIG_DESCRIPTOR_MEDIA_TYPE
        or type(expected_config_descriptor_digest) is not str
        or _OCI_DESCRIPTOR_DIGEST_RE.fullmatch(expected_config_descriptor_digest)
        is None
        or type(expected_config_descriptor_size) is not int
        or not 2 <= expected_config_descriptor_size <= _MAX_CONFIG_JSON_BYTES
        or expected_config_descriptor_size != len(raw_config_bytes)
    ):
        _fail()
    config_digest = hashlib.sha256(raw_config_bytes).hexdigest()
    if expected_config_descriptor_digest != f"sha256:{config_digest}":
        _fail()
    return config_digest


def derive_phase_a_v5_expanded_oci_config_claim_v1(
    raw_config_bytes: bytes,
    expected_config_descriptor_media_type: str,
    expected_config_descriptor_digest: str,
    expected_config_descriptor_size: int,
) -> PhaseAV5ExpandedOciConfigCommitmentClaimV1:
    """Derive an unsigned V5 claim after exact config-content tuple binding only."""

    config = _parse_canonical_json_object(raw_config_bytes)
    config_digest = _validate_expected_descriptor(
        raw_config_bytes,
        expected_config_descriptor_media_type,
        expected_config_descriptor_digest,
        expected_config_descriptor_size,
    )
    if set(config) != {"architecture", "config", "os", "rootfs"}:
        _fail()
    if config["architecture"] != "amd64" or config["os"] != "linux":
        _fail()
    _validate_rootfs(config["rootfs"])
    if type(config["config"]) is not dict:
        _fail()
    image_config = cast(dict[str, object], config["config"])
    if set(image_config) != {"Cmd", "Entrypoint", "Env", "User", "WorkingDir"}:
        _fail()
    _bounded_argv(image_config["Cmd"], entrypoint=False)
    _bounded_argv(image_config["Entrypoint"], entrypoint=True)
    user, uid, gid = _validate_user(image_config["User"])
    working_dir = _validate_working_dir(image_config["WorkingDir"])
    env_entries, env_names, env_byte_count = _validate_env(image_config["Env"])

    policy_bytes = _sequence_bytes(tuple(sorted(_RESERVED_DELIVERY_ENV_NAMES)))
    return PhaseAV5ExpandedOciConfigCommitmentClaimV1(
        schema_version="rsd.phase-a-v5-expanded-oci-config-commitment-claim.v1",
        signed_worker_attestation_scope="phase-a-v5-expanded-oci-config-profile",
        non_authorizing=True,
        oci_config_descriptor_media_type=_OCI_CONFIG_DESCRIPTOR_MEDIA_TYPE,
        oci_config_descriptor_digest=expected_config_descriptor_digest,
        oci_config_descriptor_size=expected_config_descriptor_size,
        runtime_uid=uid,
        runtime_gid=gid,
        user_commitment_sha256=_bound_hash(
            _USER_DOMAIN, config_digest, user.encode("ascii")
        ),
        user_byte_count=len(user.encode("ascii")),
        working_dir_commitment_sha256=_bound_hash(
            _WORKING_DIR_DOMAIN, config_digest, working_dir.encode("ascii")
        ),
        working_dir_byte_count=len(working_dir.encode("ascii")),
        environment_sequence_commitment_sha256=_bound_hash(
            _ENV_SEQUENCE_DOMAIN, config_digest, _sequence_bytes(env_entries)
        ),
        environment_names_commitment_sha256=_bound_hash(
            _ENV_NAMES_DOMAIN, config_digest, _sequence_bytes(env_names)
        ),
        environment_entry_count=len(env_entries),
        environment_rendered_byte_count=env_byte_count,
        reserved_delivery_env_policy_commitment_sha256=_bound_hash(
            _RESERVED_POLICY_DOMAIN, config_digest, policy_bytes
        ),
        reserved_delivery_env_names_absent=True,
    )


def _strictly_reparse_claim(
    claim: object,
) -> PhaseAV5ExpandedOciConfigCommitmentClaimV1:
    """Reject bypassed Pydantic construction before canonical claim comparison."""

    if type(claim) is not PhaseAV5ExpandedOciConfigCommitmentClaimV1:
        _fail()
    raw_claim = claim.__dict__
    if (
        type(raw_claim) is not dict
        or len(raw_claim) != len(_CLAIM_FIELD_NAMES)
        or set(raw_claim) != _CLAIM_FIELD_NAMES
        or claim.__pydantic_extra__ is not None
    ):
        _fail()
    if (
        any(type(raw_claim[field]) is not str for field in _CLAIM_STRING_FIELDS)
        or any(type(raw_claim[field]) is not int for field in _CLAIM_INTEGER_FIELDS)
        or any(type(raw_claim[field]) is not bool for field in _CLAIM_BOOLEAN_FIELDS)
    ):
        _fail()
    try:
        return PhaseAV5ExpandedOciConfigCommitmentClaimV1.model_validate(
            {field: raw_claim[field] for field in _CLAIM_FIELD_NAMES}, strict=True
        )
    except ValueError:
        _fail()


def verify_phase_a_v5_expanded_oci_config_claim_v1(
    raw_config_bytes: bytes,
    expected_config_descriptor_media_type: str,
    expected_config_descriptor_digest: str,
    expected_config_descriptor_size: int,
    claim: object,
) -> PhaseAV5ExpandedOciConfigCommitmentClaimV1:
    """Rederive and compare a V5 claim; this does not prove manifest placement."""

    derived = derive_phase_a_v5_expanded_oci_config_claim_v1(
        raw_config_bytes,
        expected_config_descriptor_media_type,
        expected_config_descriptor_digest,
        expected_config_descriptor_size,
    )
    reparsed = _strictly_reparse_claim(claim)
    if _canonical_json(reparsed.model_dump(mode="json")) != _canonical_json(
        derived.model_dump(mode="json")
    ):
        _fail()
    return derived
