# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Latest completed reconciliation per host."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRunCompletedEvent


class ModelWorktreeReconcileHostRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    correlation_id: UUID
    scanned: int
    removed: int
    pinned_and_removed: int
    kept: int
    needs_human: int
    failures: int
    freed_bytes: int
    needs_human_paths: tuple[str, ...]
    finished_at: datetime


class ModelWorktreeReconcileProjectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelWorktreeReconcileRunCompletedEvent
    current: ModelWorktreeReconcileHostRow | None = None
