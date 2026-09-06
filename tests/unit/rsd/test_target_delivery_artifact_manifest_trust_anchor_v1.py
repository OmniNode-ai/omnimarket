# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical-parser coverage for the shared public B2 trust anchor."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from omnimarket.rsd.target_delivery_artifact_manifest_trust_anchor_v1 import (
    TargetDeliveryArtifactManifestError,
    TargetDeliveryArtifactManifestTrustAnchorV1,
    parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json,
    target_delivery_artifact_manifest_trust_anchor_v1_canonical_json,
)


def _anchor() -> TargetDeliveryArtifactManifestTrustAnchorV1:
    public_key = bytes(range(32))
    return TargetDeliveryArtifactManifestTrustAnchorV1(
        schema_version="rsd.target-delivery-artifact-manifest-trust-anchor.v1",
        key_id="public-b2-root",
        public_key_base64=base64.b64encode(public_key).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(public_key).hexdigest(),
        authority_identity_sha256=hashlib.sha256(b"b2-authority").hexdigest(),
        independence_domain_identity_sha256=hashlib.sha256(
            b"b2-independence"
        ).hexdigest(),
        algorithm="ed25519",
    )


@pytest.mark.unit
def test_anchor_canonical_json_round_trips_exactly() -> None:
    anchor = _anchor()
    payload = target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(anchor)

    assert (
        parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(payload)
        == anchor
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        (b'{"algorithm":"ed25519","algorithm":"ed25519"}',),
        (b"{}",),
        (b"[]",),
        (
            json.dumps(
                _anchor().model_dump(mode="json"),
                separators=(",", ": "),
                sort_keys=True,
            ).encode("ascii"),
        ),
    ],
)
def test_anchor_parser_rejects_tampered_or_noncanonical_json(payload: bytes) -> None:
    with pytest.raises(TargetDeliveryArtifactManifestError) as raised:
        parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(payload)

    assert raised.value.phase == "parse"
