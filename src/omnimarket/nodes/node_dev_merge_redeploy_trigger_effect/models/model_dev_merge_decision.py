# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the trigger decided for one merge, and what the verify step reads back."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.dev_merge_redeploy import (
    EnumDevMergeOutcome,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_start_command import (
    ModelDevMergeStartCommand,
)


class ModelDevMergeDecision(BaseModel):
    """The trigger's decision. ``command`` is set exactly when ``outcome`` is ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    outcome: EnumDevMergeOutcome | None
    command: ModelDevMergeStartCommand | None = None
    runtime_paths: int = Field(default=0, ge=0)
    message: str = Field(default="", max_length=2000)


class ModelDevMergePending(BaseModel):
    """A published command awaiting the lane's completion and the read-back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelRepoMerged
    decision: ModelDevMergeDecision


class ModelDevMergeVerdict(BaseModel):
    """What the verify step read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumDevMergeOutcome
    final_phase: str = ""
    lane_revision: str = ""
    message: str = Field(default="", max_length=2000)


__all__ = ["ModelDevMergeDecision", "ModelDevMergePending", "ModelDevMergeVerdict"]
