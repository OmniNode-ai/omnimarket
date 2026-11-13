# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed error event of a finding (merge_without_override or detector_unreadable)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_override_alarm_effect.models.enum_merge_override_verdict import (
    EnumMergeOverrideVerdict,
)


class ModelMergeOverrideFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumMergeOverrideVerdict
    repository: str
    pr_number: int = Field(ge=1)
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    merger: str | None = None
    source: Literal["pr-merged", "repo-merged"]
    reason: str | None = None
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    correlation_id: UUID
