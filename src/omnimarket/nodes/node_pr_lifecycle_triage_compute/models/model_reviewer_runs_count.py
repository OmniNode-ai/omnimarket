# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile-review runs queued or running on open PRs' current heads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReviewerRunsCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    in_flight: int = Field(ge=0)


__all__: list[str] = ["ModelReviewerRunsCount"]
