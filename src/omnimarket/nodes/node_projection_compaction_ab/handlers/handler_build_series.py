# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-build scoreboard series and the floor ratchet.

The series carries no hypothesis test and no Holm correction. It records the
calibration split only; a claim that a selected build helps is made by a single
confirmatory run on the held-out split.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_compaction_ab.models import (
    ModelFloorFile,
    load_floor_file,
)
from omnimarket.nodes.node_projection_compaction_ab.models.model_floor_and_spec import (
    EnumDirection,
)


class ModelBuildSeriesEntry(BaseModel):
    """One build's replay report on the calibration split."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    node_commit: str = Field(min_length=1)
    split: str = Field(pattern="^calibration$")
    scores: dict[str, float]
    recorded_at: datetime


class ModelFloorViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str
    direction: EnumDirection
    floor: float
    score: float | None


def check_floor(
    entry: ModelBuildSeriesEntry, floor_file: ModelFloorFile | None = None
) -> tuple[ModelFloorViolation, ...]:
    """Criteria moved past their floor in the unfavourable direction.

    Empty until the first HELPS verdict exists. A criterion with a floor and no
    score in the entry is a violation: missing evidence is not a pass.
    """
    floor_file = floor_file or load_floor_file()
    if not floor_file.helps_verdict_seen:
        return ()
    violations: list[ModelFloorViolation] = []
    for name, criterion in floor_file.criteria.items():
        if criterion.floor is None:
            continue
        score = entry.scores.get(name)
        worse = score is None or (
            score < criterion.floor
            if criterion.direction is EnumDirection.HIGHER_IS_BETTER
            else score > criterion.floor
        )
        if worse:
            violations.append(
                ModelFloorViolation(
                    criterion=name,
                    direction=criterion.direction,
                    floor=criterion.floor,
                    score=score,
                )
            )
    return tuple(violations)


class ModelBuildSeriesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: ModelBuildSeriesEntry
    existing: tuple[ModelBuildSeriesEntry, ...] = ()


class ModelBuildSeriesResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    appended: bool
    violations: tuple[ModelFloorViolation, ...]


class HandlerBuildSeries:
    """Append a build once per (candidate_id, node_commit) and run the ratchet."""

    def handle(self, request: ModelBuildSeriesRequest) -> ModelBuildSeriesResult:
        key = (request.entry.candidate_id, request.entry.node_commit)
        known = {(e.candidate_id, e.node_commit) for e in request.existing}
        return ModelBuildSeriesResult(
            appended=key not in known, violations=check_floor(request.entry)
        )


class InMemoryBuildSeries:
    """One entry per (candidate_id, node_commit); a repeat key is ignored."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ModelBuildSeriesEntry] = {}

    def append(self, entry: ModelBuildSeriesEntry) -> bool:
        key = (entry.candidate_id, entry.node_commit)
        if key in self._entries:
            return False
        self._entries[key] = entry
        return True

    def entries(self) -> tuple[ModelBuildSeriesEntry, ...]:
        return tuple(self._entries.values())


__all__ = [
    "HandlerBuildSeries",
    "InMemoryBuildSeries",
    "ModelBuildSeriesEntry",
    "ModelBuildSeriesRequest",
    "ModelBuildSeriesResult",
    "ModelFloorViolation",
    "check_floor",
]
