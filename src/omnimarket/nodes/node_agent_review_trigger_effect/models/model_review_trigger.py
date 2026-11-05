# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment settings and the trigger's explicit observations and outcome."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelReviewTriggerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_owner: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    required_review_checks: tuple[str, ...] = Field(min_length=1)
    reviewer_workflow: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    reviewer_jobs: tuple[str, ...] = Field(min_length=1)
    bus_lane: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    lock_poll_seconds: float = Field(gt=0)
    read_limit: int = Field(gt=0, le=300)
    log_tail_bytes: int = Field(gt=0, le=65536)
    state_subdirectory: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")

    def qualify_repo(self, repo: str) -> str:
        """Watcher events use short names; webhook events already carry a slug."""
        owner, separator, name = repo.partition("/")
        if not separator:
            return f"{self.repository_owner}/{repo}"
        if owner.casefold() == self.repository_owner.casefold():
            return f"{self.repository_owner}/{name}"
        return repo


class ModelReviewTriggerEvidence(BaseModel):
    """Facts from one completed job of an exact-head workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    required: bool
    log_text: str


class ModelReviewTriggerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["emitted", "unchanged"]
    reason: str
    command_id: str
