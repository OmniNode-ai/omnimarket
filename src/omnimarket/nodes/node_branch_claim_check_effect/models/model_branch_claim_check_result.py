# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal branch claim evaluation, including delivery evidence."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_branch_claim_check_effect.models.enum_branch_claim_outcome import (
    EnumBranchClaimOutcome,
)


class ModelBranchClaimCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    pr_number: int
    head_sha: str | None
    head_ref: str | None
    ticket: str | None
    outcome: EnumBranchClaimOutcome
    conclusion: str | None
    holder_lane: str | None = None
    holder_fence: int | None = None
    holder_row: str | None = None
    claimed_at: str | None = None
    last_activity_at: str | None = None
    lanes: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    cause: str | None = None
    rows_read: int = 0
    window_since: datetime | None = None
    window_until: datetime | None = None
    window_missing_claim_rows: int = 0
    check_name: str
    check_posted: bool = False
    post_error: str | None = None
