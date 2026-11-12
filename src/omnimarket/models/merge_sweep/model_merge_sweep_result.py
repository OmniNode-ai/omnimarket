# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the merge sweep handler (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.merge_sweep.enum_pr_track import EnumPRTrack
from omnimarket.models.merge_sweep.model_classified_pr import ModelClassifiedPR
from omnimarket.models.merge_sweep.model_failure_history_summary import (
    ModelFailureHistorySummary,
)


class ModelMergeSweepResult(BaseModel):
    """Output of the merge sweep handler."""

    model_config = ConfigDict(extra="forbid")

    classified: list[ModelClassifiedPR] = Field(default_factory=list)
    status: str = "nothing_to_merge"
    failure_history_summary: ModelFailureHistorySummary = Field(
        default_factory=ModelFailureHistorySummary
    )

    @property
    def track_a_update(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_UPDATE]

    @property
    def track_a_merge(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_MERGE]

    @property
    def track_a_resolve(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_RESOLVE]

    @property
    def track_b_polish(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.B_POLISH]

    @property
    def skipped(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.SKIP]


__all__ = ["ModelMergeSweepResult"]
