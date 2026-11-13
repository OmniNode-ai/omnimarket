# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment facts the runtime edge needs to resolve a caller tenant from a bearer token."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelEdgeIdentityOverlay(BaseModel):
    """``node_inference_credentials/overlay.yaml``: how the edge verifies a caller.

    Every field is required: no value has a default in code, so an overlay that omits
    one is refused instead of silently running on a built-in setting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    jwks_url: str = Field(
        ..., min_length=1, description="Where the token issuer publishes its keys."
    )
    issuer: str = Field(..., min_length=1, description="Expected `iss` claim.")
    audience: str = Field(..., min_length=1, description="Expected `aud` claim.")
    algorithms: tuple[str, ...] = Field(
        ..., min_length=1, description="Accepted signature algorithms."
    )
    tenant_id_claim: str = Field(..., min_length=1)
    tenant_slug_claim: str = Field(..., min_length=1)
    principal_id_claim: str = Field(..., min_length=1)
    jwks_cache_seconds: int = Field(
        ..., gt=0, description="How long a fetched key set is reused."
    )
    leeway_seconds: int = Field(..., ge=0, description="Clock skew tolerated on exp.")

    @field_validator("jwks_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("jwks_url must be an http(s) url")
        return value

    @field_validator("algorithms")
    @classmethod
    def _no_unsigned(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            name.lower() == "none" or name.upper().startswith("HS") for name in value
        ):
            raise ValueError("algorithms must be asymmetric signature algorithms")
        return value


__all__: list[str] = ["ModelEdgeIdentityOverlay"]
