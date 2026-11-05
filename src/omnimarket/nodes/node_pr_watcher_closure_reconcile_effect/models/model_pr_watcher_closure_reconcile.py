# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and read models of the PR watcher closure reconciler."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PrState = Literal["OPEN", "MERGED", "CLOSED"]


class ModelPrNodeState(BaseModel):
    """GitHub's answer for one pull request node id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    state: PrState
    merged_at: str | None = None
    closed_at: str | None = None


class ModelPrWatcherClosureReconcileRequest(BaseModel):
    """One reconcile tick over the watcher state file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The watcher state file; the lock and the cursor live beside it.
    state_path: str
    # Watcher keys (``<repo name>#<n>``) the REST open-pulls lists returned this
    # tick. They are open by GitHub's own word and are not read again. The
    # search index is never consulted: a record it still lists is read anyway.
    rest_open_keys: tuple[str, ...] = ()
    # The tick's clock, ISO 8601 UTC; the cursor stamps each read with it.
    now: str
    # Overrides of the contract policy; None takes the contract value.
    batch_size: int | None = Field(default=None, ge=1, le=100)
    max_batches: int | None = Field(default=None, ge=1)


class ModelClosureFlip(BaseModel):
    """One record moved out of the open set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    state: PrState
    merged_at: str | None = None
    closed_at: str | None = None


class ModelPrWatcherClosureReconcileResult(BaseModel):
    """What one tick read, spent and flipped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # OPEN records outside the REST open lists that carry a node id.
    candidates: int
    # Records read this tick and GraphQL calls spent (one per batch).
    read_keys: tuple[str, ...]
    calls: int
    flipped: tuple[ModelClosureFlip, ...]
    # OPEN records with no node id: they cannot be batch-read and are named.
    no_node_id: tuple[str, ...]
    # Node ids GitHub answered null for (deleted or unreachable).
    unanswered: tuple[str, ...]
    # Upper bound, in ticks, before every current candidate has been read.
    ticks_to_full_pass: int
    # A transport failure is observable, rather than a successful empty pass.
    read_failed: bool = False


class ModelPrWatcherClosureTickRequest(BaseModel):
    """Bus callers supply correlation, never a host path, clock or token."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID


class ModelPrWatcherClosureObservation(BaseModel):
    """Only the observation clock is needed; PR facts come from the locked file."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    observed_at: datetime


class ModelPrWatcherClosureOverlay(BaseModel):
    """Deployment-owned snapshot and read identity; no packaged host defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    state_path: Path
    github_token_secret_ref: str = Field(min_length=1)


class ModelPrWatcherClosureTickResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    source_tick: str
    status: Literal["reconciled", "already-reconciled", "stale-state", "read-failed"]
    reconciliation: ModelPrWatcherClosureReconcileResult | None = None
    terminal_failure_cause: Literal["read-failed"] | None = Field(
        default=None,
        description="A failed batch routes the result to the declared failure terminal.",
    )


__all__ = [
    "ModelClosureFlip",
    "ModelPrNodeState",
    "ModelPrWatcherClosureObservation",
    "ModelPrWatcherClosureOverlay",
    "ModelPrWatcherClosureReconcileRequest",
    "ModelPrWatcherClosureReconcileResult",
    "ModelPrWatcherClosureTickRequest",
    "ModelPrWatcherClosureTickResult",
    "PrState",
]
