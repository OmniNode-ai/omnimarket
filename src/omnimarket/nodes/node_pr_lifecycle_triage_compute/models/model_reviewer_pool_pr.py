# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One PR's landing facts as the reviewer-pool rule reads them; every other key rides through untouched."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelReviewerPoolPr(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    pr: str
    head_sha: str
    created_at: str


__all__: list[str] = ["ModelReviewerPoolPr"]
