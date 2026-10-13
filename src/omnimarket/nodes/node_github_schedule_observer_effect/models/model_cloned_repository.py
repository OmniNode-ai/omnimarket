# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the observer reads from a clone (OMN-20803)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelClonedWorkflow(BaseModel):
    """One scheduled workflow file at the clone's head, with its cron expressions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="For example .github/workflows/x.yml.")
    crons: tuple[str, ...] = Field(min_length=1)


class ModelClonedRepository(BaseModel):
    """A clone's head, its remote default branch's head and its scheduled workflows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(min_length=1)
    remote_default_head_sha: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    workflows: tuple[ModelClonedWorkflow, ...] = ()

    @property
    def is_current(self) -> bool:
        """True when the clone's head is its remote default branch's head."""
        return self.head_sha == self.remote_default_head_sha
