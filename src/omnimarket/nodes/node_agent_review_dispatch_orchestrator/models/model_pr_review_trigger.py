# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR observations, deployment scope and the proactive review request result."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .model_agent_review import ModelAgentReviewScmBus


class ModelPrReviewObservation(BaseModel):
    """The common PR identity in webhook and poller observations.

    Head and branch are hints only. Every request re-reads the live PR through
    the source-control gateway before publishing a review command.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int = Field(gt=0)
    head_sha: str | None = None


class ModelPrReviewTriggerConfig(BaseModel):
    """The deployment selects repositories and branches, never shipped defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repositories: tuple[str, ...] = Field(min_length=1)
    base_branches: tuple[str, ...] = Field(min_length=1)
    scm_bus: ModelAgentReviewScmBus
    read_timeout_seconds: float = Field(gt=0)
    state_subdirectory: str = Field(pattern=r"^[A-Za-z0-9_-]+$")


class ModelPrReviewTriggerResult(BaseModel):
    """A published command, an idempotent repeat, or a named non-eligible PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["requested", "unchanged"]
    reason: str
    command_id: str = ""
