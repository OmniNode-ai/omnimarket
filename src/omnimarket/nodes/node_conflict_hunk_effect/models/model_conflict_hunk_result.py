# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_conflict_hunk_effect [OMN-8991]."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelConflictHunkResult(BaseModel):
    """Outcome of a conflict-hunk resolution attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    files_resolved: list[str] = Field(default_factory=list)
    resolution_committed: bool
    is_noop: bool
    correlation_id: UUID
    error: str | None = None
