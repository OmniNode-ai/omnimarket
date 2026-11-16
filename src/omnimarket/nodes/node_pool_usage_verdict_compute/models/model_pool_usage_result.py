# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of one pool usage verdict evaluation.

Verdicts and reasons are closed vocabularies. ``drain_labels`` stays empty
unless at least one label is ok: the pool is never drained empty, so a ``drain``
label only skips NEW lane starts while another label is still ok.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

WindowState = Literal["ok", "drain", "limited", "unread", "reset_passed"]
Verdict = Literal["ok", "drain", "limited", "unknown"]
Reason = Literal[
    "within_thresholds",
    "five_hour_drain",
    "weekly_drain",
    "five_hour_limited",
    "weekly_limited",
    "pool_limited_mark",
    "revoked",
    "no_reading",
    "stale_reading",
]


class ModelPoolWindowUsage(BaseModel):
    """One window of a label's verdict: where it stands and how far to its reset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fraction_used: float | None
    seconds_to_reset: int | None
    state: WindowState


class ModelPoolLabelUsage(BaseModel):
    """The verdict for one label, both windows reported, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    five_hour: ModelPoolWindowUsage
    weekly: ModelPoolWindowUsage
    verdict: Verdict
    reason: Reason
    seconds_until_usable: int | None = None


class ModelPoolUsageResult(BaseModel):
    """Every label judged, the drain set, and the verdict counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labels: tuple[ModelPoolLabelUsage, ...]
    drain_labels: tuple[str, ...]
    ok: int
    drain: int
    limited: int
    unknown: int
    correlation_id: UUID


__all__ = [
    "ModelPoolLabelUsage",
    "ModelPoolUsageResult",
    "ModelPoolWindowUsage",
]
