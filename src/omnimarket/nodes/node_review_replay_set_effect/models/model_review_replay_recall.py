# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen recall bar and the recall score of a roster over the replay set (OMN-22971)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumRosterVerdict(StrEnum):
    """What one roster member said about one replayed head."""

    BLOCKED = "blocked"
    PASSED = "passed"
    DEGRADED = "degraded"


class ModelReplayLabel(BaseModel):
    """Whether a frozen item is known to carry a defect, and the record that says so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    defective: bool
    evidence: str = Field(
        min_length=1,
        description="The record that settles the label, such as a later fix.",
    )


class ModelRecallBar(BaseModel):
    """The bar a roster must reach and the labels recall is measured against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "review_replay_recall_bar.v1"
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    min_recall: float = Field(gt=0.0, le=1.0)
    labels: tuple[ModelReplayLabel, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _measurable(self) -> ModelRecallBar:
        ids = [label.item_id for label in self.labels]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicate label for item: {', '.join(duplicated)}")
        if not any(label.defective for label in self.labels):
            raise ValueError("no item is labelled defective, so recall is undefined")
        return self


class ModelFreezeRecallBarRequest(BaseModel):
    """Freeze the bar and labels beside the replay set they belong to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    store_root: str = Field(min_length=1)
    bar: ModelRecallBar


class ModelFreezeRecallBarResult(BaseModel):
    """Where the bar was frozen and its hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bar_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bar_path: str
    already_frozen: bool


class ModelRosterObservation(BaseModel):
    """One roster member's verdict on one replayed head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    member: str = Field(min_length=1)
    verdict: EnumRosterVerdict


class ModelReviewReplayRecallRequest(BaseModel):
    """Score a roster's verdicts on the frozen replay set against the frozen bar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    store_root: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    roster_ref: str = Field(min_length=1)
    expected_bar_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="The bar hash the caller recorded when it froze the bar; the run refuses another.",
    )
    observations: tuple[ModelRosterObservation, ...]


class ModelReviewReplayRecallResult(BaseModel):
    """Recall of the roster over the replay set; ``passed`` is false below the frozen bar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    roster_ref: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bar_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    min_recall: float
    defective_items: int
    caught: int
    recall: float
    passed: bool
    missed: tuple[str, ...] = Field(
        description="Defective items no roster member blocked, degraded answers included."
    )
    false_blocks: int = Field(
        ge=0, description="Items labelled not defective that a member blocked."
    )


__all__ = [
    "EnumRosterVerdict",
    "ModelFreezeRecallBarRequest",
    "ModelFreezeRecallBarResult",
    "ModelRecallBar",
    "ModelReplayLabel",
    "ModelReviewReplayRecallRequest",
    "ModelReviewReplayRecallResult",
    "ModelRosterObservation",
]
