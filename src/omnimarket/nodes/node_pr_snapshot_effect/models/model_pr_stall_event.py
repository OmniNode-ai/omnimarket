# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrStallEvent — represents a PR that has been shape-identical across consecutive snapshots."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelPrStallEvent(BaseModel):
    """A PR detected as stalled — shape-identical across two consecutive snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(description="GitHub PR number.")
    repo: str = Field(description="GitHub repo in org/repo format.")
    stall_count: int = Field(
        description="Number of consecutive identical snapshots observed (minimum 2)."
    )
    blocking_reason: str = Field(
        description="Human-readable summary of the blocking fields (e.g. 'required_checks_pass=False, merge_state_status=BLOCKED')."
    )
    first_seen_at: datetime = Field(
        description="Timestamp of the first snapshot where this stall was detected."
    )
    last_seen_at: datetime = Field(
        description="Timestamp of the most recent snapshot confirming the stall."
    )
    head_sha: str | None = Field(
        default=None,
        description="HEAD SHA at the time of stall detection.",
    )


__all__: list[str] = ["ModelPrStallEvent"]
