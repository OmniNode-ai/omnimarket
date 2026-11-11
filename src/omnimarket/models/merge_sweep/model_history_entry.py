# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-run failure tracking for a single PR (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFailureHistoryEntry(BaseModel):
    """Cross-run failure tracking for a single PR."""

    model_config = ConfigDict(extra="forbid")

    first_seen: str
    last_seen: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_polishes: int = 0
    last_failure_categories: list[str] = Field(default_factory=list)
    last_result: str | None = None
    last_run_id: str | None = None
    runs_seen: list[str] = Field(default_factory=list)


__all__ = ["ModelFailureHistoryEntry"]
