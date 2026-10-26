# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Version-one target request and receipt for the delegation call effect."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDelegationGeneration(BaseModel):
    """Caller-contract defaults and explicit per-request overrides.

    An unset field stays unset; this model supplies no generation defaults.
    Route capability evidence must cover every supplied field before dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: tuple[str, ...] | None = None
    seed: int | None = None
    response_format: dict[str, Any] | None = None
    tools: tuple[dict[str, Any], ...] | None = None
    tool_choice: dict[str, Any] | str | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    reasoning_effort: str | None = None
    thinking: dict[str, Any] | None = None

    def overridden_by(
        self, other: ModelDelegationGeneration
    ) -> ModelDelegationGeneration:
        """Merge non-null overrides, preserving zero/false across wire round trips."""
        return type(self).model_validate(
            self.model_dump(exclude_none=True)
            | other.model_dump(exclude_unset=True, exclude_none=True)
        )


class ModelDelegationTargetRequest(BaseModel):
    """A logical target and brief; endpoint and credential authority stay off wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    target: Literal["model-call", "claude-code", "codex", "claude-glm", "antigravity"]
    brief: str = Field(min_length=1)
    call_site: str = Field(min_length=1)
    repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    generation: ModelDelegationGeneration = ModelDelegationGeneration()
    timeout_seconds: float | None = Field(default=None, gt=0, le=10800)
    agent_profile: str | None = None

    @model_validator(mode="after")
    def _session_checkout(self) -> Self:
        if self.target != "model-call" and (not self.repo or not self.commit_sha):
            raise ValueError("agent sessions require repo and an exact commit_sha")
        if self.target != "model-call" and self.generation.model_dump(
            exclude_none=True
        ):
            raise ValueError("generation parameters apply only to model-call targets")
        return self


class ModelDelegationTargetResult(BaseModel):
    """Session or inference outcome, with a durable receipt reference for sessions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    target: str
    status: Literal["completed", "failed", "timed_out", "duplicate"]
    content: str = ""
    exit_code: int | None = None
    session_id: str | None = None
    model_used: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    worktree: str | None = None
    commit_sha: str | None = None
    head_sha: str | None = None
    receipt_path: str | None = None
    output_path: str | None = None
    duration_seconds: float = Field(default=0, ge=0)
    detail: str = ""
    request_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


__all__ = [
    "ModelDelegationGeneration",
    "ModelDelegationTargetRequest",
    "ModelDelegationTargetResult",
]
