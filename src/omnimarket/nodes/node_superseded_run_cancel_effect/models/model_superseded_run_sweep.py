# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one supersede sweep of a repository (OMN-17427)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_superseded_run_cancel_effect.models.model_superseded_run import (
    ModelSupersededRunCancelledEvent,
)


class ModelSupersededRunSweepRequest(BaseModel):
    """Sweep one repository's queued and in-progress runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str = Field(description="owner/name")
    dry_run: bool = Field(
        default=False, description="Decide and report; cancel nothing."
    )


class ModelSupersededRunSkip(BaseModel):
    """A selected run GitHub did not cancel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    http_status: int
    detail: str


class ModelSupersededRunSweepResult(BaseModel):
    """What one sweep read, decided and cancelled.

    cancelled holds one event per cancelled run (in dry_run, per run that
    would be). read_error is set when the run list could not be read, and
    nothing is cancelled then.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    dry_run: bool
    runs_read: int
    cancelled: tuple[ModelSupersededRunCancelledEvent, ...] = ()
    not_cancelled: tuple[ModelSupersededRunSkip, ...] = ()
    unjudged_merge_group_runs: int = 0
    read_error: str | None = None
