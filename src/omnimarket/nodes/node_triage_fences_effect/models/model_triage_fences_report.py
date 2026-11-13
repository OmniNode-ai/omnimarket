# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fence report and its correlated terminal (OMN-20682).

The report's field names and order are the JSON the triage lanes consume.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.events.triage_fences import (
    ModelTriageFencesClaim,
    ModelTriageFencesCounts,
    ModelTriageFencesPullFence,
)


class ModelTriageFencesReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str
    ledger: str
    fenced_tickets: tuple[str, ...]
    open_claims: tuple[ModelTriageFencesClaim, ...]
    stale_claims: tuple[ModelTriageFencesClaim, ...]
    stale_after_hours: float
    open_pr_fences: tuple[ModelTriageFencesPullFence, ...]
    unparsed_rows: tuple[str, ...]
    errors: tuple[str, ...]
    counts: ModelTriageFencesCounts


class ModelTriageFencesResult(BaseModel):
    """exit_code 0 carries the report; any other exit code carries ``error`` and no report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    exit_code: int
    report: ModelTriageFencesReport | None = None
    error: str = ""
