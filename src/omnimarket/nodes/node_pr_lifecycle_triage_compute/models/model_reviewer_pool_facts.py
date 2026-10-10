# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open PRs with a reviewer-pool red, the reruns spent and the reviewer endpoint's free slots."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_pr import (
    ModelReviewerPoolPr,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_state_record import (
    ModelReviewerPoolStateRecord,
)


class ModelReviewerPoolFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prs: tuple[ModelReviewerPoolPr, ...]
    state_records: tuple[ModelReviewerPoolStateRecord, ...] = ()
    in_flight: int
    slots: int = 4


__all__: list[str] = ["ModelReviewerPoolFacts"]
