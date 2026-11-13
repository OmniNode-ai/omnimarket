# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One verdict per merge, whatever it is; the node's terminal event."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_override_alarm_effect.models.enum_merge_override_verdict import (
    EnumMergeOverrideVerdict,
)


class ModelMergeOverrideVerdictEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int = Field(ge=1)
    verdict: EnumMergeOverrideVerdict
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    merger: str | None = None
    source: Literal["pr-merged", "repo-merged"]
    receipt_seen: bool = Field(
        description="A landing receipt existed for the PR, whatever its head."
    )
    ruling_row_id: str | None = None
    reason: str | None = None
    correlation_id: UUID
    decided_at: AwareDatetime
