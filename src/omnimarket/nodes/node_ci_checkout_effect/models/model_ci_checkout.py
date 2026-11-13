# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O and overlay of node_ci_checkout_effect.

The bus carries the request and reply only; the bytes move over git from the served
source URL the reply names. Every deployment setting lives in :class:`ModelCiCheckoutOverlay`;
no key has a default, so an absent key refuses the load and names it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

OVERLAY_ID = "node_ci_checkout_effect"
OVERLAY_FILE_ENV = "ONEX_CI_CHECKOUT_OVERLAY"
#: The host the handler answers for when the runtime builds it with no arguments (the serve passes its own).
HOST_NAME_ENV = "ONEX_CI_CHECKOUT_HOST_NAME"


class ModelCiCheckoutBusProfile(BaseModel):
    """The in-memory default or the Redpanda lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inmemory", "kafka"]
    lane: str | None = Field(default=None, min_length=1)
    #: The environment variable naming the broker address when the lane declaration does not supply it.
    kafka_bootstrap_env: str | None = Field(default=None, min_length=1)
    #: The environment variable naming the workspace root the lane declaration is read from.
    workspace_root_env: str | None = Field(default=None, min_length=1)


class ModelCiCheckoutSource(BaseModel):
    """One git-served source: a bare mirror or canonical clone on this host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    path_template: str = Field(min_length=1)
    serve_url_template: str = Field(min_length=1)

    @field_validator("path_template")
    @classmethod
    def _path_template_has_repo_name(cls, v: str) -> str:
        if "{repo_name}" not in v:
            raise ValueError("path_template must contain '{repo_name}'")
        return v

    @field_validator("serve_url_template")
    @classmethod
    def _serve_url_template_has_repo_name(cls, v: str) -> str:
        if "{repo_name}" not in v:
            raise ValueError("serve_url_template must contain '{repo_name}'")
        return v


class ModelCiCheckoutOverlay(BaseModel):
    """Every setting a checkout answers from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    github_owner: str = Field(min_length=1)
    sources: tuple[ModelCiCheckoutSource, ...] = Field(min_length=1)
    sync_on_miss: bool
    #: The host's clone sync, an argv template with {repo} (owner/name) and {ref}; empty
    #: only when a miss is not synced.
    sync_command: tuple[str, ...]
    sync_timeout_seconds: float = Field(gt=0)
    git_timeout_seconds: float = Field(gt=0)
    reply_timeout_seconds: float = Field(gt=0)
    default_profile: str = Field(min_length=1)
    profiles: dict[str, ModelCiCheckoutBusProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def _sync_command_present_when_syncing(self) -> ModelCiCheckoutOverlay:
        if self.sync_on_miss and not self.sync_command:
            raise ValueError("sync_command is required when sync_on_miss is true")
        return self

    def profile(self, name: str | None) -> ModelCiCheckoutBusProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(
                f"no bus profile {key!r}; the overlay has {sorted(self.profiles)}"
            )
        return self.profiles[key]


class EnumCiCheckoutStatus(StrEnum):
    SERVED = "served"
    SYNCED_ON_MISS = "synced_on_miss"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    FAILED = "failed"


class ModelCiCheckoutRequest(BaseModel):
    """One checkout request over the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str | None = None
    requested_by_lane: str = Field(min_length=1)
    host: str = Field(min_length=1, max_length=64)


class ModelCiCheckoutResult(BaseModel):
    """The checkout's terminal event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    status: EnumCiCheckoutStatus
    repo: str
    sha: str
    source_name: str | None = None
    serve_url: str | None = None
    upstream_fetches: int = Field(ge=0)
    detail: str | None = None

    @field_validator("serve_url")
    @classmethod
    def _serve_url_consistent(cls, v: str | None, info: ValidationInfo) -> str | None:
        status = info.data.get("status")
        source_name = info.data.get("source_name")
        if status in (
            EnumCiCheckoutStatus.SERVED,
            EnumCiCheckoutStatus.SYNCED_ON_MISS,
        ):
            if v is None or source_name is None:
                raise ValueError(
                    "served/synced_on_miss require serve_url and source_name"
                )
        else:
            if v is not None:
                raise ValueError("other statuses require serve_url None")
        return v


__all__ = [
    "HOST_NAME_ENV",
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "EnumCiCheckoutStatus",
    "ModelCiCheckoutBusProfile",
    "ModelCiCheckoutOverlay",
    "ModelCiCheckoutRequest",
    "ModelCiCheckoutResult",
    "ModelCiCheckoutSource",
]
