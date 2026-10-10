# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PRs after the reviewer-pool rule, in the order given."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_pr import (
    ModelReviewerPoolPr,
)


class ModelReviewerPoolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prs: tuple[ModelReviewerPoolPr, ...]


__all__: list[str] = ["ModelReviewerPoolResult"]
