# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review gateway's request, key choice and dry-run plan (OMN-21014).

A Hostile Review voter is an OpenAI-compatible endpoint named in the review
voters overlay. This gateway is one such endpoint on a lab host: it spends only
the Anthropic keys catalogued for its tenant and bound to its own host
(OMN-21012), so a review placed on a host is paid by the Claude account on that
host (operator ruling 2026-10-11T17:58:45Z). Nothing here holds a key value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.projection.credential_publisher import LAB_HOST_NAME_PATTERN
from omnimarket.routing.byok_provider_backends import ANTHROPIC_WORKSPACE_ID_PATTERN


class ModelReviewGatewayKey(BaseModel):
    """One catalogued key the gateway may spend: a reference, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    bound_host: str = Field(pattern=LAB_HOST_NAME_PATTERN)
    expires_at: datetime | None = None
    workspace_id: str | None = Field(
        default=None,
        pattern=ANTHROPIC_WORKSPACE_ID_PATTERN,
        description="The Anthropic workspace the key belongs to (OMN-20967), if any.",
    )


class ModelClaudeReviewGatewayRequest(BaseModel):
    """Serve (or, with ``dry_run``, plan) the gateway for one host and tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(
        pattern=LAB_HOST_NAME_PATTERN,
        description="This machine's lab host table row: the only keys it spends.",
    )
    tenant_id: str = Field(min_length=1, description="The tenant whose keys these are.")
    model: str = Field(
        pattern=r"^claude-[a-z0-9.-]+$",
        description="The Claude model every review runs on, served as /v1/models.",
    )
    cloud_refused_repositories: tuple[str, ...] = Field(
        min_length=1,
        description="Repositories whose diffs never go to a cloud model, from a private overlay.",
    )
    bind_host: str = Field(default="127.0.0.1", min_length=1)
    bind_port: int = Field(default=8140, ge=1, le=65535)
    thinking_mode: Literal["disabled", "adaptive"] = Field(
        default="disabled",
        description="Native thinking protocol supported by this gateway's model, from its host overlay.",
    )
    upstream_timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    dry_run: bool = False

    @field_validator("cloud_refused_repositories")
    @classmethod
    def validate_refused_repositories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        repositories = tuple(name.strip() for name in value)
        if any(not name for name in repositories):
            raise ValueError(
                "cloud_refused_repositories entries must be non-empty strings"
            )
        return repositories


class ModelReviewGatewayPlan(BaseModel):
    """What one review would do, with no upstream call (the dry run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    tenant_id: str
    model: str
    upstream_url: str
    candidate_key_refs: tuple[str, ...]
    chosen_key_ref: str | None
    chosen_key_name: str | None
    translated_request: dict[str, Any]
    would_answer: int = Field(description="200 when a key would serve, else 503.")
    reason: str


__all__ = [
    "ModelClaudeReviewGatewayRequest",
    "ModelReviewGatewayKey",
    "ModelReviewGatewayPlan",
]
