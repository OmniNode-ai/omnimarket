# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the policy-bound calculation: the narrowed-run bound, shard count and full-run crossover."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyBoundInputError(ValueError):
    """The calculation cannot run: durations or the test-file listing could not be read."""


class EnumBoundMergeKind(StrEnum):
    #: A merge the replay narrows; it counts toward the share over a bound.
    NARROWED = "narrowed"
    #: A merge that stays on the full suite by design (a dependency or workflow change); never counted.
    FULL_SUITE = "full_suite"


class ModelBoundMerge(BaseModel):
    """One replayed merge: the test paths its narrowed selection covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(min_length=1)
    selected_paths: tuple[str, ...] = Field(min_length=1)
    kind: EnumBoundMergeKind


class ModelPolicyBoundRequest(BaseModel):
    """The replay inputs of the calculation. Setup and collection seconds are measurements, never defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(min_length=1)
    merges: tuple[ModelBoundMerge, ...] = Field(min_length=1)
    #: Measured seconds one shard spends before its first test; None while unmeasured.
    shard_setup_seconds: float | None = Field(default=None, ge=0)
    #: Measured seconds of the whole-tree collection gate a narrowed run adds; None while unmeasured.
    collection_gate_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _both_measurements_or_neither(self) -> ModelPolicyBoundRequest:
        if (self.shard_setup_seconds is None) != (self.collection_gate_seconds is None):
            raise ValueError(
                "shard_setup_seconds and collection_gate_seconds are given together or not at all"
            )
        return self


class ModelBoundOption(BaseModel):
    """What one candidate bound does to the replay: merges over it and the test-seconds it saves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bound_seconds: float = Field(ge=0)
    #: ``policy``, ``comparison`` or ``crossover``.
    source: str
    merges_over_bound: int = Field(ge=0)
    merges_under_bound: int = Field(ge=0)
    test_seconds_saved: float = Field(ge=0)


class ModelMergeCost(BaseModel):
    """One merge's selected seconds, shard count and estimated runner seconds (None where unmeasured)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    kind: EnumBoundMergeKind
    selected_seconds: float = Field(ge=0)
    untimed_file_count: int = Field(ge=0)
    #: Shards of a narrowed run under the policy bound; None when it takes the full suite.
    shards: int | None = Field(default=None, ge=1)
    narrowed_runner_seconds: float | None = Field(default=None, ge=0)
    full_runner_seconds: float | None = Field(default=None, ge=0)


class ModelPolicyBoundResult(BaseModel):
    """The one calculation: derived from policy.yaml, the durations file and the replay inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_total_seconds: float = Field(ge=0)
    full_split_count: int = Field(ge=1)
    target_times_split_seconds: float = Field(ge=0)
    suite_total_within_tolerance: bool
    untimed_file_seconds: float = Field(ge=0)
    untimed_file_seconds_p90: float = Field(ge=0)
    narrowed_bound_seconds: float = Field(ge=0)
    narrowed_merge_count: int = Field(ge=0)
    bound_options: tuple[ModelBoundOption, ...]
    merge_costs: tuple[ModelMergeCost, ...]
    cost_columns_measured: bool
    #: Largest selection the shard cap carries: ``max_narrowed_shards * target_shard_seconds``.
    shard_cap_seconds: float = Field(ge=0)
    #: Largest selected-seconds figure whose narrowed run still costs less than the full run; None while unmeasured.
    crossover_selected_seconds: float | None = Field(default=None, ge=0)
    #: Shas of narrowed merges whose estimated narrowed cost is not below the full run's.
    merges_costing_more_than_full: tuple[str, ...]
    files_without_entry: tuple[str, ...]
    entries_without_file: tuple[str, ...]
