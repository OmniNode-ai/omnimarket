"""Contracts for a supplied, non-authorizing V4 profile envelope."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelV4StaticProfileValidationInput(BaseModel):
    """Canonical envelope and externally pinned profile-root bytes only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_envelope_canonical_json: bytes = Field(min_length=1, max_length=270_336)
    profile_trust_anchor_canonical_json: bytes = Field(min_length=1, max_length=8_192)


class ModelV4StaticProfileValidationOutput(BaseModel):
    """Static identity evidence; this output cannot authorize delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    non_authorizing: Literal[True] = True
    effects_allowed: Literal[False] = False
