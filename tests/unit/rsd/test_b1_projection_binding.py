"""Immutable public-vector parity for offline B1 projection binding."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from omnimarket.nodes.node_rsd_b1_projection_binding_validate_compute.handlers.handler_b1_projection_binding import (
    validate_b1_projection_binding,
)
from omnimarket.nodes.node_rsd_b1_projection_binding_validate_compute.models.model_b1_projection_binding import (
    ModelB1ProjectionBindingValidationInput,
)
from omnimarket.rsd import b1_projection_binding as binding
from omnimarket.rsd import historical_target_delivery_map_v1 as historical
from omnimarket.rsd import v4_static_profile as static

_VECTOR = (
    Path(__file__).parents[2]
    / "fixtures/rsd/target_delivery_map_projection_binding_public_vector.yaml"
)


def _decoded(vector: dict[str, object], key: str) -> bytes:
    encoded = cast(dict[str, object], vector[key])
    assert encoded["encoding"] == "standard_base64_fixed_segments_v1"
    segments = cast(list[str], encoded["segments"])
    assert segments
    assert all(type(segment) is str for segment in segments)
    compact = "".join(segments)
    assert [
        compact[index : index + 76] for index in range(0, len(compact), 76)
    ] == segments
    return base64.b64decode(compact, validate=True)


def _vector() -> dict[str, object]:
    parsed = yaml.safe_load(_VECTOR.read_bytes())
    assert type(parsed) is dict
    return cast(dict[str, object], parsed)


def test_immutable_public_vector_validates_without_authority() -> None:
    assert hashlib.sha256(_VECTOR.read_bytes()).hexdigest() == (
        "5955a5788ab7360e23e85fa221962cdf63318759f48f051d5746add9d2770893"
    )
    vector = _vector()
    delivery_map = historical.parse_historical_target_delivery_map_v1_canonical_json(
        _decoded(vector, "target_delivery_map_canonical_json_utf8_base64")
    )
    projection = (
        static.parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
            _decoded(vector, "static_delivery_projection_canonical_json_utf8_base64")
        )
    )
    envelope = (
        static.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            _decoded(vector, "profile_envelope_canonical_json_utf8_base64")
        )
    )
    policy = binding.parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
        _decoded(vector, "trust_policy_canonical_json_utf8_base64")
    )
    signed = binding.parse_target_delivery_map_projection_binding_v1_canonical_json(
        _decoded(vector, "binding_canonical_json_utf8_base64")
    )
    acceptance = binding.validate_target_delivery_map_projection_binding_v1(
        delivery_map=delivery_map,
        static_delivery_projection=projection,
        profile_envelope=envelope,
        binding=signed,
        trust_policy=policy,
    )
    assert acceptance.build_allowed is False
    assert acceptance.materialization_allowed is False
    assert acceptance.attach_allowed is False
    assert acceptance.effect_allowed is False
    output = validate_b1_projection_binding(
        ModelB1ProjectionBindingValidationInput(
            historical_target_delivery_map_canonical_json=_decoded(
                vector, "target_delivery_map_canonical_json_utf8_base64"
            ),
            static_delivery_projection_canonical_json=_decoded(
                vector, "static_delivery_projection_canonical_json_utf8_base64"
            ),
            profile_envelope_canonical_json=_decoded(
                vector, "profile_envelope_canonical_json_utf8_base64"
            ),
            binding_canonical_json=_decoded(
                vector, "binding_canonical_json_utf8_base64"
            ),
            trust_policy_canonical_json=_decoded(
                vector, "trust_policy_canonical_json_utf8_base64"
            ),
        )
    )
    assert output.non_authorizing is True
    assert output.evidence_effect_allowed is False


def test_public_vector_rejects_tampered_binding_signature() -> None:
    vector = _vector()
    signed = binding.parse_target_delivery_map_projection_binding_v1_canonical_json(
        _decoded(vector, "binding_canonical_json_utf8_base64")
    )
    with pytest.raises(binding.TargetDeliveryMapProjectionBindingError):
        binding.validate_target_delivery_map_projection_binding_v1(
            delivery_map=historical.parse_historical_target_delivery_map_v1_canonical_json(
                _decoded(vector, "target_delivery_map_canonical_json_utf8_base64")
            ),
            static_delivery_projection=static.parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
                _decoded(
                    vector, "static_delivery_projection_canonical_json_utf8_base64"
                )
            ),
            profile_envelope=static.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
                _decoded(vector, "profile_envelope_canonical_json_utf8_base64")
            ),
            binding=signed.model_copy(update={"signature_base64": "A" * 88}),
            trust_policy=binding.parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
                _decoded(vector, "trust_policy_canonical_json_utf8_base64")
            ),
        )


@pytest.mark.parametrize(
    ("model_key", "strict"),
    [
        (
            "trust_policy_canonical_json_utf8_base64",
            binding.strict_canonical_target_delivery_map_projection_binding_trust_policy_v1,
        ),
        (
            "binding_canonical_json_utf8_base64",
            binding.strict_canonical_target_delivery_map_projection_binding_v1,
        ),
    ],
)
def test_b1_strict_models_reject_hidden_deleted_and_fieldset_state(
    model_key: str, strict: Callable[[object], object]
) -> None:
    vector = _vector()
    parser = (
        binding.parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json
        if model_key.startswith("trust")
        else binding.parse_target_delivery_map_projection_binding_v1_canonical_json
    )
    model = parser(_decoded(vector, model_key))
    for mutation in ("hidden", "deleted", "fieldset"):
        changed = model.model_copy()
        if mutation == "hidden":
            object.__setattr__(changed, "__pydantic_private__", {"unexpected": True})
        elif mutation == "deleted":
            del changed.__dict__[next(iter(type(changed).model_fields))]
        else:
            object.__setattr__(changed, "__pydantic_fields_set__", {"bogus"})
        with pytest.raises(binding.TargetDeliveryMapProjectionBindingError):
            strict(changed)


def test_b1_acceptance_canonicalization_rejects_hidden_state() -> None:
    vector = _vector()
    acceptance = binding.parse_target_delivery_map_projection_binding_acceptance_v1_canonical_json(
        _decoded(vector, "acceptance_canonical_json_utf8_base64")
    ).model_copy()
    object.__setattr__(acceptance, "__pydantic_private__", {"unexpected": True})
    with pytest.raises(binding.TargetDeliveryMapProjectionBindingError):
        binding.target_delivery_map_projection_binding_acceptance_v1_canonical_json(
            acceptance
        )
