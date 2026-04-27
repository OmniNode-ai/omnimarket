# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for persisted dispatch-worker spec artifacts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCompiledDispatchWorker(BaseModel):
    """Compiled worker prompt/spec emitted by node_dispatch_worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validated_task_description: str = Field(..., min_length=1)
    validated_prompt_template: str = Field(..., min_length=1)
    proposed_agent_spawn_args: dict[str, str] = Field(default_factory=dict)
    collision_fence_embeds: tuple[str, ...] = Field(default=())
    rejected_reason: str = Field(default="")

    @field_validator("proposed_agent_spawn_args")
    @classmethod
    def validate_spawn_args(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            return value
        required = {"name", "team_name", "model", "subagent_type"}
        missing = sorted(required - set(value))
        if missing:
            msg = f"proposed_agent_spawn_args missing required keys: {missing}"
            raise ValueError(msg)
        return value


class ModelDispatchWorkerSpecArtifact(BaseModel):
    """Persisted spec artifact written by session Phase 3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    dispatch_id: str = Field(..., min_length=1)
    correlation_chain: str = Field(..., min_length=1)
    compiled_at: datetime
    dispatch_worker: ModelCompiledDispatchWorker


__all__ = ["ModelCompiledDispatchWorker", "ModelDispatchWorkerSpecArtifact"]
