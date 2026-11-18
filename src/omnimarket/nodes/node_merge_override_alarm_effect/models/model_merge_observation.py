# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One merge as either merge topic announced it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelMergeObservation(BaseModel):
    """Neither merge event carries the head or the merger; the GitHub read does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int = Field(ge=1)
    merge_sha: str | None = None
    source: Literal["pr-merged", "repo-merged"]
