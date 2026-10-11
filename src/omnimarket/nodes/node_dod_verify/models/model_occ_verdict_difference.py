# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen verdicts and comparison output for OCC retirement S5 (OMN-20072)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)

type OccDifferenceOutcome = Literal[
    "agree",
    "negative_control_refused",
    "expected_difference",
    "forbidden_difference",
    "unclassified_difference",
    "not_compared",
    "unknown",  # OMN-20917: S7 replay only; S5 classify never returns it.
]


class ModelOccVerdict(BaseModel):
    """OCC admission, or an unavailable check-run conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool | None
    conclusion: str | None = None
    reason: str | None = None


class ModelNewPathVerdict(BaseModel):
    """Aggregate receipt-gate verdict and the first refused ticket's reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    reason: str | None = None


class ModelOccVerdictDifferenceResult(BaseModel):
    """Whether the difference itself is allowed, with both verdicts preserved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    outcome: OccDifferenceOutcome
    reason_code: EnumOccVerdictDifferenceReason | None
    old_admitted: bool | None
    new_admitted: bool | None
    old_reason: str | None
    new_reason: str | None
    message: str
