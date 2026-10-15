# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two events node_e2e_orchestrator consumes from other workflows.

Both keep unknown fields: the producers add fields over time and the evidence bundle records the
payload as received.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.e2e_bridge import (
    DEFAULT_CI_INTERVAL_SECONDS,
    DEFAULT_CI_TIMEOUT_SECONDS,
)


class ModelE2eBuildCompleted(BaseModel):
    """A build loop finished; ``pr_refs`` are the PRs it opened (owner/repo#number)."""

    model_config = ConfigDict(frozen=True, extra="allow")

    correlation_id: UUID
    pr_refs: tuple[str, ...] = ()
    cost_event_keys: tuple[str, ...] = ()
    completed_at: datetime | None = None
    dry_run: bool = Field(
        default=False,
        description="True makes the PR lifecycle start command non-acting (no arm, no merge).",
    )
    ci_poll_timeout_seconds: int = Field(default=DEFAULT_CI_TIMEOUT_SECONDS, ge=1)
    ci_poll_interval_seconds: int = Field(default=DEFAULT_CI_INTERVAL_SECONDS, ge=1)


class ModelE2ePrLifecycleCompleted(BaseModel):
    """The PR lifecycle orchestrator finished a sweep."""

    model_config = ConfigDict(frozen=True, extra="allow")

    correlation_id: UUID
    prs_merged: int = 0
    prs_fixed: int = 0
    final_state: str = "COMPLETE"


__all__: list[str] = ["ModelE2eBuildCompleted", "ModelE2ePrLifecycleCompleted"]
