# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared GitHub event payload models for cross-node consumption.

The ``onex.evt.github.pr-merged.v1`` event is published by the
``pr-merged-publisher`` GHA workflow on every repo when a PR merges (OMN-13226 /
T2) and consumed by ``node_pr_merged_projection`` (OMN-13227 / T3), which
materializes it into the ``pr_merged_events`` projection so the per-machine
worktree reaper (OMN-13228 / T4) can poll
``GET /projection/onex.evt.github.pr-merged.v1?since=<cursor>`` to discover newly
merged PRs and reap their worktrees.

Projection consumers import the payload model from here instead of reaching into
the node's private handler module.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrMergedEvent(BaseModel):
    """Inbound payload of ``onex.evt.github.pr-merged.v1``.

    Mirrors the canonical payload built by ``scripts/publish_pr_merged_event.py``
    (``build_payload``): the publisher emits ``{event_id, topic, repo, branch,
    pr_number, ticket, merged_at, published_at}``. The projection materializes
    the worktree-matching subset ``{repo, branch, pr_number, ticket, merged_at}``
    plus the publisher ``event_id`` (the dedup key) into the projection row.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    branch: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    ticket: str = Field(default="")
    merged_at: str = Field(..., min_length=1)
    published_at: str | None = Field(default=None)


__all__ = ["ModelPrMergedEvent"]
