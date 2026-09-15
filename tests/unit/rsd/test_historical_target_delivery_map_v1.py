"""Focused fail-closed coverage for historical V1 map public roots."""

from __future__ import annotations

import base64
import hashlib

import pytest

from omnimarket.rsd.historical_target_delivery_map_v1 import (
    TargetDeliveryMapSignerTrustAnchorV1,
    _strict,
)


def _anchor() -> TargetDeliveryMapSignerTrustAnchorV1:
    key = bytes(range(32))
    return TargetDeliveryMapSignerTrustAnchorV1(
        schema_version="rsd.target-delivery-map-signer-trust-anchor.v1",
        key_id="historical-map-root",
        public_key_base64=base64.b64encode(key).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(key).hexdigest(),
        algorithm="ed25519",
    )


@pytest.mark.unit
def test_strict_root_rejects_hidden_deleted_and_cyclic_state() -> None:
    anchor = _anchor()
    hidden = anchor.model_copy()
    object.__setattr__(hidden, "__pydantic_private__", {"unexpected": True})
    with pytest.raises(ValueError, match="model is invalid"):
        _strict(hidden, TargetDeliveryMapSignerTrustAnchorV1)

    deleted = anchor.model_copy()
    del deleted.__dict__["algorithm"]
    with pytest.raises(ValueError, match="model is invalid"):
        _strict(deleted, TargetDeliveryMapSignerTrustAnchorV1)

    fieldset_drift = anchor.model_copy()
    object.__setattr__(fieldset_drift, "__pydantic_fields_set__", {"bogus"})
    with pytest.raises(ValueError, match="model is invalid"):
        _strict(fieldset_drift, TargetDeliveryMapSignerTrustAnchorV1)

    cyclic = anchor.model_copy()
    cyclic.__dict__["key_id"] = cyclic
    with pytest.raises((RecursionError, ValueError)):
        _strict(cyclic, TargetDeliveryMapSignerTrustAnchorV1)
