# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and read models of the PR watcher closure reconciler."""

from __future__ import annotations

from typing import Literal

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


__all__ = [
    "ModelClosureFlip",
    "ModelPrNodeState",
    "ModelPrWatcherClosureReconcileRequest",
    "ModelPrWatcherClosureReconcileResult",
    "PrState",
]
