# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the test-selection selector (OMN-23296)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumSelectorOutcome(StrEnum):
    #: The selection runs narrowed, split into ``split_count`` shards.
    NARROWED = "narrowed"
    #: The selection needs more shards than a narrowed run may take: it takes the full-suite matrix.
    FULL_SUITE_TOO_HEAVY = "full_suite_too_heavy"
    #: The recorded durations could not be read, so the weight is unknown: it takes the full-suite matrix.
    FULL_SUITE_DURATIONS_UNREAD = "full_suite_durations_unread"


class ModelSelectorPolicy(BaseModel):
    """Every setting of the selector, read from ``policy.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Estimated test seconds one shard of a narrowed selection carries; the full suite's own share
    #: (the recorded suite total over ``full_split_count``), held within ``record_tolerance_ratio`` by a test.
    target_shard_seconds: int = Field(ge=1)
    #: Most shards a narrowed selection may take.
    max_narrowed_shards: int = Field(ge=1)
    #: Estimated test seconds above which a selection takes the full suite (the operator's narrowed-run bound).
    narrowed_bound_seconds: int = Field(ge=1)
    #: Seconds a test file with no duration entry costs: the durations file's p90.
    untimed_file_seconds: float = Field(ge=0)
    #: Allowed relative gap between ``target_shard_seconds * full_split_count`` and the recorded suite total,
    #: and between ``untimed_file_seconds`` and the durations file's p90.
    record_tolerance_ratio: float = Field(gt=0, lt=1)
    #: Bounds the policy-bound report prices beside the policy bound.
    comparison_bounds_seconds: tuple[int, ...]
    durations_path: str = Field(min_length=1)
    path_count_shards: tuple[tuple[int, int], ...]
    path_count_max_shards: int = Field(ge=1)
    #: The stage gate: a narrowing rule is active only at or above its stage; 0 is shadow.
    selection_stage: int = Field(ge=0, le=4)
    #: Shards of the full-suite matrix, which a flag-off run and every escalation take.
    full_split_count: int = Field(ge=1, le=40)
    #: Days a pull request's selection record artifact is kept.
    record_retention_days: int = Field(ge=1, le=90)

    @model_validator(mode="after")
    def _ladder_is_ordered(self) -> ModelSelectorPolicy:
        if (
            self.narrowed_bound_seconds
            > self.max_narrowed_shards * self.target_shard_seconds
        ):
            raise ValueError(
                "narrowed_bound_seconds may not exceed max_narrowed_shards * target_shard_seconds"
            )
        bounds = [paths for paths, _ in self.path_count_shards]
        if bounds != sorted(set(bounds)):
            raise ValueError("path_count_shards must rise by selected path count")
        if (
            max(
                [shards for _, shards in self.path_count_shards]
                + [self.path_count_max_shards]
            )
            > self.max_narrowed_shards
        ):
            raise ValueError("the path-count rule may not exceed max_narrowed_shards")
        return self


class ModelSelectorRequest(BaseModel):
    """One narrowed selection to size: its test paths and the checkout the durations are read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_paths: tuple[str, ...] = Field(min_length=1)
    repo_root: str = Field(min_length=1)


class ModelSelectorDecision(BaseModel):
    """The shard count (or the escalation) the selector answers, with the estimate it rests on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSelectorOutcome
    #: Shards of a narrowed run; None when the selection takes the full-suite matrix.
    split_count: int | None = Field(default=None, ge=1)
    #: Recorded serial seconds the selected paths cover; None when the durations were unread.
    estimated_seconds: float | None = Field(default=None, ge=0)
    #: Estimated seconds one shard carries (estimated_seconds / split_count); None on escalation.
    shard_seconds: float | None = Field(default=None, ge=0)

    @property
    def escalates_to_full_suite(self) -> bool:
        return self.outcome is not EnumSelectorOutcome.NARROWED

    @model_validator(mode="after")
    def _split_count_only_when_narrowed(self) -> ModelSelectorDecision:
        if self.escalates_to_full_suite != (self.split_count is None):
            raise ValueError(
                "split_count is set exactly when the selection is narrowed"
            )
        return self


class ModelSelectorFailed(BaseModel):
    """Published when a request cannot be graded at all (an unreadable payload)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    terminal_failure_cause: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        """The runtime's failure-terminal guard reads ``terminal_failure_cause``."""
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data
