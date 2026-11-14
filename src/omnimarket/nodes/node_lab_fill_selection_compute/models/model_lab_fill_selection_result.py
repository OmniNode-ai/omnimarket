# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable lab-fill decisions and next baselines (OMN-20662)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lab_fill import ModelLabFillPrLandPlan

from .enum_lab_fill_skip_reason import EnumLabFillSkipReason
from .model_lab_fill_selection_request import ModelLabFillInputBaseline


@dataclass(frozen=True, slots=True)
class ModelLabFillDecision:
    """The first matching gate for one candidate, or eligibility."""

    key: str
    ticket: str
    pr: str
    reason: EnumLabFillSkipReason | None
    caller_reason: str
    detail: str
    # The repository the lane works in and the evidence it came from (OMN-17427).
    repo: str = ""
    repo_source: str = ""


class ModelLabFillSelectionResult(BaseModel):
    """Decisions in candidate order and retained input baselines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[ModelLabFillDecision, ...]
    baselines: tuple[ModelLabFillInputBaseline, ...]
    # The idle-slot pr-land fallback's plan (OMN-20864); None when the request carried no PR facts.
    pr_land: ModelLabFillPrLandPlan | None = None
    # Implementation-merged tickets taken as DoD-verify work for the slots ordinary and pr-land
    # work leave idle, one per ticket in candidate order (OMN-17427).
    dod_verify: tuple[ModelLabFillDecision, ...] = ()
    # Tickets held because an earlier lane ended already-built or skipped-owned with a merged PR, listed
    # once per tick for the proof-close path instead of a fill lane (OMN-22347).
    closer: tuple[ModelLabFillDecision, ...] = ()
