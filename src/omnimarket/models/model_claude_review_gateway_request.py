# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review gateway's request, shared by the gateway and key-probe nodes (OMN-21014)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.handlers.network_bindings import network_host
from omnimarket.projection.credential_publisher import LAB_HOST_NAME_PATTERN


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
    bind_host: str = Field(
        default_factory=lambda: network_host("loopback.ipv4"), min_length=1
    )
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


__all__ = ["ModelClaudeReviewGatewayRequest"]
