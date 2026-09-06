"""Typed boundary for effect-free V4 artifact-evidence verification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelV4ArtifactEvidenceValidationInput(BaseModel):
    """Canonical bytes supplied by the caller; the node never collects them."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    closure_canonical_json: bytes = Field(min_length=1, max_length=393_216)
    worker_trust_policy_canonical_json: bytes = Field(min_length=1, max_length=8_192)
    profile_envelope_canonical_json: bytes = Field(min_length=1, max_length=270_336)
    profile_trust_anchor_canonical_json: bytes = Field(min_length=1, max_length=8_192)


class ModelV4ArtifactEvidenceValidationOutput(BaseModel):
    """Acceptance evidence only; every operational permission remains false."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    non_authorizing: Literal[True] = True
    evidence_effect_allowed: Literal[False] = False
    build_allowed: Literal[False] = False
    materialization_allowed: Literal[False] = False
    attach_allowed: Literal[False] = False
