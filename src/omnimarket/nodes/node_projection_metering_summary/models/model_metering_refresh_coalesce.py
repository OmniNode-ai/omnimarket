# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The metering refresh coalescer's decision, overlay, and stats (OMN-24492).

Each refresh command makes the writer refold every stored run of its tenants.
The coalescer lets the writer run at most one day-refresh per tenant per
interval and remembers the days it held back, so a burst of terminals spends
one fold instead of one per terminal.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)


class EnumMeteringRefreshVerdict(StrEnum):
    """Whether the coalescer runs a refresh or holds it back."""

    RUN = "run"
    COALESCED = "coalesced"


class ModelMeteringRefreshCoalesceOverlay(BaseModel):
    """Deployment overlay: the minimum spacing, in seconds, between day refreshes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_interval_seconds: int = Field(..., ge=0)


class ModelMeteringRefreshCoalesceDecision(BaseModel):
    """One ``admit`` outcome: the refresh to run, or the evidence it was held.

    ``request`` is the command to run, merged with the held-back days the run
    carries; None when the verdict is COALESCED. ``settles`` names, per tenant,
    the held-back days this run carries; ``complete`` removes them from the
    pending set. ``tenants`` names the tenants whose last run this admission
    set; ``rollback`` undoes it so a failed run retries at once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumMeteringRefreshVerdict
    request: ModelMeteringSummaryRefreshRequested | None
    admitted_at: AwareDatetime
    settles: tuple[tuple[str, frozenset[date]], ...] = ()
    tenants: tuple[str, ...] = ()


class ModelMeteringRefreshCoalesceStats(BaseModel):
    """Counts and the held-back days, for tests and monitoring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int
    coalesced: int
    pending: dict[str, frozenset[date]]


__all__: list[str] = [
    "EnumMeteringRefreshVerdict",
    "ModelMeteringRefreshCoalesceDecision",
    "ModelMeteringRefreshCoalesceOverlay",
    "ModelMeteringRefreshCoalesceStats",
]
