# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied importance weights for the compaction loss measurement."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_Weight = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class ModelCompactionLossWeights(BaseModel):
    """Positive per-item weights; unit defaults preserve the unweighted measure.

    A caller may give selected kinds greater importance without changing what
    counts as a surviving item. Absent kinds contribute no weight to a score.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_claim: _Weight = 1.0
    promise: _Weight = 1.0
    ruling: _Weight = 1.0
    pr: _Weight = 1.0
    ticket: _Weight = 1.0
    operator_instruction: _Weight = 1.0
    operator_mode: _Weight = 1.0

    def for_kind(self, kind: str) -> float:
        return float(getattr(self, kind))


__all__ = ["ModelCompactionLossWeights"]
