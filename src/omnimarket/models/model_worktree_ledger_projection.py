# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings for the work-ledger projection source of a worktree reconcile command."""

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerProjectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bus_lane: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    page_size: int = Field(default=200, ge=1, le=1000)
    max_pages: int = Field(default=500, ge=1)
    required_kinds: tuple[str, ...] = Field(
        default=("CLAIM", "TERMINAL", "RELEASE"), min_length=1
    )
    unmeasured_days_allowed: int = Field(default=1, ge=0)
