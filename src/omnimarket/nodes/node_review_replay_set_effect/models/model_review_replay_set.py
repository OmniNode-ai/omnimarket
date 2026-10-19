# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, candidates, manifest and result of the review replay set (OMN-20910)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.review_replay.model_replay_manifest import (
    ModelReplayItem,
    ModelReplayManifest,
)

SIZE_BINS = ("small", "medium", "large")


class ModelReplayRepo(BaseModel):
    """One repository the set draws from: its name and its canonical clone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    clone_path: str = Field(min_length=1)
    base_ref: str = Field(
        default="origin/HEAD",
        description="The landing branch; origin/HEAD resolves to the clone's default branch.",
    )


class ModelPinnedItem(BaseModel):
    """A merged PR the set must contain, such as a diff already measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr_number: int = Field(ge=1)


class ModelMergedCandidate(BaseModel):
    """One squash-merged PR on a landing branch, before its diff is read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(ge=1)
    merge_sha: str = Field(min_length=7)
    parent_sha: str = Field(min_length=7)
    title: str
    changed_lines: int = Field(ge=0)


class ModelReviewReplaySetRequest(BaseModel):
    """Freeze ``target_count`` merged diffs, stratified by repo and size."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repos: tuple[ModelReplayRepo, ...] = Field(min_length=1)
    store_root: str = Field(min_length=1)
    target_count: int = Field(default=40, ge=1)
    seed: str = Field(min_length=1)
    history_limit: int = Field(
        default=300, ge=1, description="First-parent commits scanned per repo."
    )
    size_bin_upper_lines: tuple[int, int] = Field(
        default=(199, 1000),
        description="Changed lines: small up to the first, medium up to the second, large above.",
    )
    pinned: tuple[ModelPinnedItem, ...] = ()

    @model_validator(mode="after")
    def _bins_ascend(self) -> ModelReviewReplaySetRequest:
        low, high = self.size_bin_upper_lines
        if not 0 < low < high:
            raise ValueError("size_bin_upper_lines must be two ascending positive ints")
        names = [r.repo for r in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("a repo appears twice")
        if len(self.pinned) > self.target_count:
            raise ValueError("more pinned items than target_count")
        return self


class ModelReviewReplaySetResult(BaseModel):
    """What was frozen, where, and under which hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_path: str
    item_count: int
    short_by: int = Field(ge=0, description="target_count less the items frozen.")
    strata: dict[str, int] = Field(description="Items per '<repo>/<size bin>'.")
    already_frozen: bool


__all__ = [
    "SIZE_BINS",
    "ModelMergedCandidate",
    "ModelPinnedItem",
    "ModelReplayItem",
    "ModelReplayManifest",
    "ModelReplayRepo",
    "ModelReviewReplaySetRequest",
    "ModelReviewReplaySetResult",
]
