# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run the supersede rule selected, and the event for one cancelled (OMN-17427)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_superseded_run_cancel_effect.models.enum_superseded_reason import (
    EnumSupersededReason,
)


class ModelSupersededRun(BaseModel):
    """One run to cancel, with the fact that supersedes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    workflow_name: str
    event: str
    head_sha: str
    reason: EnumSupersededReason
    pr_number: int | None = None
    current_head_sha: str | None = None
    base_branch: str | None = None


class ModelSupersededRunCancelledEvent(BaseModel):
    """Emitted once per cancelled run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    run_id: int
    reason: EnumSupersededReason
    workflow_name: str
    event: str
    head_sha: str
    pr_number: int | None = None
    current_head_sha: str | None = None
    base_branch: str | None = None
    dry_run: bool = False
