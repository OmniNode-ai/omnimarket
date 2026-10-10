# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable lab-fill decisions and next baselines (OMN-20662)."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class ModelLabFillSelectionResult:
    """Decisions in candidate order and retained input baselines."""

    decisions: tuple[ModelLabFillDecision, ...]
    baselines: tuple[ModelLabFillInputBaseline, ...]
