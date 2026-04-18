# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase 2 completion event models for the merge-sweep state reducer [OMN-8997].

These events are emitted by Phase 2 effect nodes and consumed by the reducer
to track per-PR failure history. Mirrors the pattern established by
ModelThreadRepliedEvent in node_thread_reply_effect.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelConflictResolvedEvent(BaseModel):
    """Emitted after a conflict-hunk resolution attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr_number: int
    repo: str = Field(..., description="GitHub repo slug (org/repo).")
    resolution_committed: bool = Field(
        ..., description="True if the resolved patch was committed and pushed."
    )
    is_noop: bool = Field(
        default=False,
        description="True if the PR had no actual conflicts to resolve (idempotent).",
    )
    conflict_files: list[str] = Field(default_factory=list)


__all__: list[str] = ["ModelConflictResolvedEvent"]
