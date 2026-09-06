"""Typed boundary for effect-free B1 projection-binding verification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelB1ProjectionBindingValidationInput(BaseModel):
    """Caller-supplied canonical evidence only; no map source is consulted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    historical_target_delivery_map_canonical_json: bytes = Field(
        min_length=1, max_length=393_216
    )
    static_delivery_projection_canonical_json: bytes = Field(
        min_length=1, max_length=262_144
    )
    profile_envelope_canonical_json: bytes = Field(min_length=1, max_length=270_336)
    binding_canonical_json: bytes = Field(min_length=1, max_length=393_216)
    trust_policy_canonical_json: bytes = Field(min_length=1, max_length=393_216)


class ModelB1ProjectionBindingValidationOutput(BaseModel):
    """Acceptance evidence only; every operational permission remains false."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target_delivery_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    static_delivery_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    non_authorizing: Literal[True] = True
    evidence_effect_allowed: Literal[False] = False
    build_allowed: Literal[False] = False
    materialization_allowed: Literal[False] = False
    attach_allowed: Literal[False] = False
