# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event for node_rebase_effect [OMN-8961]."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelRebaseCompletedEvent(BaseModel):
    """Emitted when a PR rebase has been attempted (success or conflict)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    success: bool
    conflict_files: list[str] = Field(default_factory=list)
    error: str | None = None
    expected_sha_before: str = ""
    actual_sha_after: str | None = None
    elapsed_seconds: float = 0.0
    base_ref_name: str = ""
    head_ref_name: str = ""
