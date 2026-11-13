# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open PRs with a reviewer-pool red, the reruns spent and the reviewer endpoint's free slots."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
    wait_merge_states: tuple[str, ...] = Field(
        default=("conflicting", "behind"),
        description=(
            "Merge states whose head changes before it can merge, so its review waits for the new head "
            "(review last, OMN-19998). The default is the contract's value; a caller passes its overlay's."
        ),
    )


__all__: list[str] = ["ModelReviewerPoolFacts"]
