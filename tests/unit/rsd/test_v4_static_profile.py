"""Parity and tamper checks for the inert RSD V4 static-profile boundary."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from omnimarket.nodes.node_rsd_v4_static_profile_validate_compute.handlers.handler_v4_static_profile import (
    validate_v4_static_profile,
)
from omnimarket.nodes.node_rsd_v4_static_profile_validate_compute.models.model_v4_static_profile import (
    ModelV4StaticProfileValidationInput,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerAttachStaticV4Error,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json,
)

_VECTOR = (
    Path(__file__).parents[2]
    / "fixtures"
    / "rsd"
    / "container_attach_static_v4_vectors.yaml"
)
_VECTOR_SHA256 = "61c7ff5383c0d7617e191476f2faf7ffb9f2829929f4736e83cdbafce4f6d627"


def _vector_bytes(name: str) -> bytes:
    vector = yaml.safe_load(_VECTOR.read_text(encoding="utf-8"))
    return base64.b64decode("".join(vector[name]["segments"]))


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


@pytest.mark.unit
def test_e853400_profile_envelope_vector_parity() -> None:
    assert hashlib.sha256(_VECTOR.read_bytes()).hexdigest() == _VECTOR_SHA256
    envelope_bytes = _vector_bytes("profile_envelope_canonical_json_utf8_base64")
    envelope = parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
        envelope_bytes
    )
    assert container_bootstrap_static_role_profile_envelope_v4_sha256(envelope) == (
        "a69ff81993d7a8bfd89fd02df0913a22913589a89749eead30e98784d43047c4"
    )
    result = validate_v4_static_profile(
        ModelV4StaticProfileValidationInput(
            profile_envelope_canonical_json=envelope_bytes,
            profile_trust_anchor_canonical_json=_vector_bytes(
                "profile_root_canonical_json_utf8_base64"
            ),
        )
    )
    assert (
        result.profile_sha256
        == "13553631f91ec17fff8ff6c758be771b77feeb78818fe0e3341a634b604e2686"
    )
    assert result.non_authorizing is True
    assert result.effects_allowed is False


@pytest.mark.unit
def test_tampered_profile_envelope_refuses_validation() -> None:
    envelope = bytearray(_vector_bytes("profile_envelope_canonical_json_utf8_base64"))
    envelope[-2] ^= 1
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=bytes(envelope),
                profile_trust_anchor_canonical_json=_vector_bytes(
                    "profile_root_canonical_json_utf8_base64"
                ),
            )
        )


@pytest.mark.unit
def test_wrong_profile_trust_anchor_refuses_valid_envelope() -> None:
    key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    anchor = json.loads(_vector_bytes("profile_root_canonical_json_utf8_base64"))
    anchor["public_key_base64"] = base64.b64encode(key).decode("ascii")
    anchor["public_key_fingerprint_sha256"] = hashlib.sha256(key).hexdigest()
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=_vector_bytes(
                    "profile_envelope_canonical_json_utf8_base64"
                ),
                profile_trust_anchor_canonical_json=_canonical_json(anchor),
            )
        )


@pytest.mark.unit
def test_signature_and_profile_hash_drift_refuse_before_any_effect() -> None:
    envelope = json.loads(_vector_bytes("profile_envelope_canonical_json_utf8_base64"))
    envelope["signature_base64"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=_canonical_json(envelope),
                profile_trust_anchor_canonical_json=_vector_bytes(
                    "profile_root_canonical_json_utf8_base64"
                ),
            )
        )
    envelope = json.loads(_vector_bytes("profile_envelope_canonical_json_utf8_base64"))
    envelope["static_role_profile_sha256"] = "0" * 64
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=_canonical_json(envelope),
                profile_trust_anchor_canonical_json=_vector_bytes(
                    "profile_root_canonical_json_utf8_base64"
                ),
            )
        )


@pytest.mark.unit
def test_duplicate_key_json_and_active_authority_symbols_are_refused() -> None:
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=(
                    b'{"schema_version":"x","schema_version":"x"}'
                ),
                profile_trust_anchor_canonical_json=_vector_bytes(
                    "profile_root_canonical_json_utf8_base64"
                ),
            )
        )
    noncanonical = json.dumps(
        json.loads(_vector_bytes("profile_envelope_canonical_json_utf8_base64")),
        indent=2,
        sort_keys=True,
    ).encode("ascii")
    with pytest.raises(ContainerAttachStaticV4Error):
        validate_v4_static_profile(
            ModelV4StaticProfileValidationInput(
                profile_envelope_canonical_json=noncanonical,
                profile_trust_anchor_canonical_json=_vector_bytes(
                    "profile_root_canonical_json_utf8_base64"
                ),
            )
        )
    source = Path("src/omnimarket/rsd/v4_static_profile.py").read_text(encoding="utf-8")
    for forbidden in (
        "TargetDeliveryMapV1",
        "project_target_delivery_map_v1_structurally",
        "ContainerAttachAuthorizationTicketV4",
        "ContainerAttachRequestV4",
    ):
        assert forbidden not in source
