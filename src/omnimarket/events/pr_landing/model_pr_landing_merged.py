# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of the merged terminal landing event."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)


class ModelPrLandingMerged(BaseModel):
    """Terminal: GitHub merged the PR. Exactly one terminal per (PR, open episode) (P4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    seq: int = Field(..., ge=0)
    episode: int = Field(
        ...,
        ge=0,
        description="The open episode this terminal ends; consumers deduplicate on (PR, episode) (F9, F10).",
    )
    merged_at: datetime = Field(...)


__all__: list[str] = ["ModelPrLandingMerged"]
