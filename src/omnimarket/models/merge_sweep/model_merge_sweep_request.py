# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input for the merge sweep handler (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.merge_sweep.model_failure_history_entry import (
    ModelFailureHistoryEntry,
)
from omnimarket.models.merge_sweep.model_pr_info import ModelPRInfo


class ModelMergeSweepRequest(BaseModel):
    """Input for the merge sweep handler."""

    model_config = ConfigDict(extra="forbid")

    prs: list[ModelPRInfo]
    require_approval: bool = True
    merge_method: str = "squash"
    max_total_merges: int = 0
    skip_polish: bool = False
    failure_history: dict[str, ModelFailureHistoryEntry] = Field(default_factory=dict)
    run_id: str = ""
    use_lifecycle_ordering: bool = Field(
        default=False,
        description=(
            "When True, reorder Track A PRs via the lifecycle pipeline "
            "(triage → reducer) for dependency-optimal merge order."
        ),
    )


__all__ = ["ModelMergeSweepRequest"]
