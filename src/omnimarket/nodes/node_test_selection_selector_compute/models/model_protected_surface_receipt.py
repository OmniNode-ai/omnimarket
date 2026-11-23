# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed receipt of the protected delegation-surface measurement.

Three merges of the replay window changed a protected surface, and the full-suite rule of the adjacency file
sends every such change to the whole suite. This receipt states, for each protected merge, the test seconds a
reverse-import-closure selection would run, alone and together with a fixed delegation test set, and compares
both with the narrowed-run bound. The fixed set replaces the whole-suite rule only when it moves a merge under
the bound; ``end_state`` says which way the measurement fell. Every figure is recomputed from the frozen
tree snapshot the replay input names, so the receipt is a derived value and never an observation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"


class EnumProtectedEndState(StrEnum):
    NO_MERGE_MOVES_UNDER_BOUND = "no_merge_moves_under_bound"
    A_MERGE_MOVES_UNDER_BOUND = "a_merge_moves_under_bound"


class ModelProtectedBound(BaseModel):
    """The narrowed-run bound at the pinned policy: the most seconds a narrowed selection may carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_shard_seconds: float = Field(gt=0)
    max_narrowed_shards: int = Field(ge=1)
    bound_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _bound_is_the_product(self) -> ModelProtectedBound:
        if self.bound_seconds != self.target_shard_seconds * self.max_narrowed_shards:
            raise ValueError(
                "bound_seconds is not target_shard_seconds times max_narrowed_shards"
            )
        return self


class ModelProtectedFixedSet(BaseModel):
    """The fixed delegation test set: two directories and the flat delegation test files under ``tests/``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paths: tuple[str, ...] = Field(min_length=1)
    seconds: float = Field(ge=0)


class ModelProtectedMerge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(pattern=_HEX40)
    in_replay_window: bool
    """True when the merge is one of the replay input's merges; false for a merge measured beside them."""
    changed_files: tuple[str, ...] = Field(min_length=1)
    closure_paths: tuple[str, ...] = Field(min_length=1)
    closure_seconds: float = Field(ge=0)
    closure_plus_fixed_paths: tuple[str, ...] = Field(min_length=1)
    closure_plus_fixed_seconds: float = Field(ge=0)
    closure_under_bound: bool
    closure_plus_fixed_under_bound: bool

    @model_validator(mode="after")
    def _fixed_set_only_adds(self) -> ModelProtectedMerge:
        if self.closure_plus_fixed_seconds < self.closure_seconds:
            raise ValueError("the fixed set cannot take seconds off the closure")
        if self.closure_under_bound and not self.closure_plus_fixed_under_bound:
            raise ValueError(
                "a closure under the bound cannot grow past it by adding the fixed set"
            )
        return self


class ModelProtectedSurfaceReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    replay_input_path: str = Field(min_length=1)
    replay_input_sha256: str = Field(pattern=_HEX64)
    tree_id: str = Field(pattern=_HEX40)
    durations_sha256: str = Field(pattern=_HEX64)
    """Digest of the durations file the frozen tree was built from."""
    durations_total_seconds: float = Field(ge=0)
    bound: ModelProtectedBound
    fixed_set: ModelProtectedFixedSet
    window_merges: int = Field(ge=1)
    """Merges of the replay input the protected merges were found among."""
    merges: tuple[ModelProtectedMerge, ...] = Field(min_length=1)
    moved_under_bound: tuple[str, ...]
    """Shas whose closure plus the fixed set is at or under the bound."""
    end_state: EnumProtectedEndState

    @model_validator(mode="after")
    def _figures_agree(self) -> ModelProtectedSurfaceReceipt:
        shas = [merge.sha for merge in self.merges]
        if len(set(shas)) != len(shas):
            raise ValueError("a merge appears twice in the receipt")
        bound = self.bound.bound_seconds
        for merge in self.merges:
            if merge.closure_under_bound != (merge.closure_seconds <= bound):
                raise ValueError(
                    f"{merge.sha}: closure_under_bound contradicts its seconds"
                )
            if merge.closure_plus_fixed_under_bound != (
                merge.closure_plus_fixed_seconds <= bound
            ):
                raise ValueError(
                    f"{merge.sha}: closure_plus_fixed_under_bound contradicts its seconds"
                )
        moved = tuple(m.sha for m in self.merges if m.closure_plus_fixed_under_bound)
        if self.moved_under_bound != moved:
            raise ValueError("moved_under_bound is not the merges under the bound")
        expected = (
            EnumProtectedEndState.A_MERGE_MOVES_UNDER_BOUND
            if moved
            else EnumProtectedEndState.NO_MERGE_MOVES_UNDER_BOUND
        )
        if self.end_state is not expected:
            raise ValueError("end_state contradicts the merges under the bound")
        if self.fixed_set.seconds > self.durations_total_seconds:
            raise ValueError("the fixed set weighs more than the whole suite")
        return self
