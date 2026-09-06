"""Canonical public signer root for the offline B2 artifact manifest.

This is the exact shared V1 trust-anchor data contract used by B2 V2.  It
contains no manifest issuance or delivery authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_MAX_DEPTH = 32
_MAX_NODES = 4_096


class TargetDeliveryArtifactManifestError(ValueError):
    """Fixed, value-redacted B2 validation failure."""

    __slots__ = ("phase",)

    def __init__(self, phase: Literal["parse", "anchor", "input", "manifest"]):
        super().__init__("target delivery artifact manifest validation failed")
        self.phase = phase


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail(phase: Literal["parse", "anchor", "input", "manifest"]) -> NoReturn:
    raise TargetDeliveryArtifactManifestError(phase)


def _b64(value: str) -> bytes:
    if type(value) is not str:
        raise ValueError("base64 is invalid")
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("base64 is invalid") from None
    if base64.b64encode(result).decode("ascii") != value:
        raise ValueError("base64 is invalid")
    return result


def _canonical(model: BaseModel, *, limit: int) -> bytes:
    try:
        payload = json.dumps(
            model.model_dump(mode="json", warnings="error"),
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


def _preflight(payload: bytes, *, limit: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= limit:
        raise ValueError("JSON is invalid")
    depth = nodes = 0
    quoted = escaped = False
    for char in payload:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            elif char < 32:
                raise ValueError("JSON is invalid")
            continue
        if char == 34:
            quoted = True
        elif char in (123, 91):
            depth += 1
        elif char in (125, 93):
            depth -= 1
        if char not in b" \t\r\n:,":
            nodes += 1
        if depth < 0 or depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("JSON is invalid")
    if quoted or escaped or depth:
        raise ValueError("JSON is invalid")


def _parse(
    payload: bytes, *, limit: int
) -> TargetDeliveryArtifactManifestTrustAnchorV1:
    _preflight(payload, limit=limit)
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
    result = TargetDeliveryArtifactManifestTrustAnchorV1.model_validate(
        value, strict=True
    )
    if _canonical(result, limit=limit) != payload:
        raise ValueError("JSON is invalid")
    return result


class TargetDeliveryArtifactManifestTrustAnchorV1(_Model):
    """Externally pinned public signer root for a B2 manifest."""

    schema_version: Literal["rsd.target-delivery-artifact-manifest-trust-anchor.v1"]
    key_id: str = Field(pattern=_IDENTIFIER)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    authority_identity_sha256: str = Field(pattern=_SHA256)
    independence_domain_identity_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_key_and_identities(self) -> Self:
        key = _b64(self.public_key_base64)
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
            or len(
                {
                    self.public_key_fingerprint_sha256,
                    self.authority_identity_sha256,
                    self.independence_domain_identity_sha256,
                }
            )
            != 3
        ):
            raise ValueError("manifest anchor is invalid")
        return self


def target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(
    anchor: TargetDeliveryArtifactManifestTrustAnchorV1,
) -> bytes:
    """Serialize the externally pinned B2 public root canonically."""
    try:
        if type(anchor) is not TargetDeliveryArtifactManifestTrustAnchorV1:
            raise ValueError
        return _canonical(anchor, limit=2_048)
    except (TypeError, ValueError):
        _fail("anchor")


def parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(
    payload: bytes,
) -> TargetDeliveryArtifactManifestTrustAnchorV1:
    """Parse only the bounded canonical B2 public root spelling."""
    try:
        return _parse(payload, limit=2_048)
    except (TypeError, ValidationError, ValueError):
        _fail("parse")
