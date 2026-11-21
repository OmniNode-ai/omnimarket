# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of change-aware test selection and of the record kept for each pull request run."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class EnumFullSuiteReason(StrEnum):
    SHARED_MODULE = "shared_module"
    THRESHOLD_MODULES = "threshold_modules"
    TEST_INFRASTRUCTURE = "test_infrastructure"
    PROTECTED_SURFACE = "protected_surface"
    UNMAPPED_MODULE = "unmapped_module"
    MAIN_BRANCH = "main_branch"
    MERGE_GROUP = "merge_group"
    SCHEDULED = "scheduled"
    FEATURE_FLAG_OFF = "feature_flag_off"
    # OMN-15277: a changed path directly under tests/ (root-level, no
    # subdirectory) cannot be narrowed below `tests/` itself -- `_resolve()`
    # would otherwise construct a non-existent pseudo-directory
    # (`tests/<file>.py/`) that the on-disk filter silently drops, or (when
    # the file happens to exist) a `TestPath` value that is file-shaped, not
    # the directory-only shape the model documents. Escalate to the real
    # full suite instead of relying on either.
    CHANGED_TEST_UNNARROWABLE = "changed_test_unnarrowable"
    # OMN-20305: the diff mapped to no test directory (a workflow-only PR, for
    # one), so the selector's only safe answer is the whole tests/ tree. That is
    # a full-suite run and takes the full-suite shard matrix; it must never be
    # a single unsplit job.
    NO_NARROWABLE_SELECTION = "no_narrowable_selection"
    # OMN-23296: node_test_selection_selector_compute found the narrowed paths too
    # heavy (or their recorded durations unreadable), so the full-suite matrix runs.
    NARROWED_SELECTION_TOO_HEAVY = "narrowed_selection_too_heavy"
    # The selection record has no entry for the policy's selection stage, so the stage gate has
    # nothing to run and the full-suite matrix runs.
    SELECTION_UNAVAILABLE = "selection_unavailable"


# OMN-17232: a directory under tests/ (trailing slash) or one test file that
# always_selected_paths names; changed-test narrowing still emits directories only.
TestPath = Annotated[
    str, StringConstraints(pattern=r"^tests(/[A-Za-z0-9_./-]+)?/$|^tests/[\w/]+\.py$")
]
ModuleName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", min_length=1)
]


class ModelTestSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_paths: list[TestPath] = Field(..., min_length=1)
    split_count: int = Field(..., ge=1, le=40)
    is_full_suite: bool
    full_suite_reason: EnumFullSuiteReason | None = Field(default=None)
    matrix: list[int] = Field(...)

    @model_validator(mode="after")
    def validate_full_suite_reason(self) -> Self:
        if self.is_full_suite and self.full_suite_reason is None:
            raise ValueError("full_suite_reason required when is_full_suite=True")
        if not self.is_full_suite and self.full_suite_reason is not None:
            raise ValueError("full_suite_reason forbidden when is_full_suite=False")
        if len(self.matrix) != self.split_count:
            raise ValueError(
                f"matrix length {len(self.matrix)} must equal split_count {self.split_count}"
            )
        return self

    @classmethod
    def full_suite(cls, reason: EnumFullSuiteReason, split_count: int) -> Self:
        """The whole tests/ tree dealt across ``split_count`` shards (policy ``full_split_count``)."""
        return cls(
            selected_paths=["tests/"],
            split_count=split_count,
            is_full_suite=True,
            full_suite_reason=reason,
            matrix=list(range(1, split_count + 1)),
        )


class EnumSelectionKind(StrEnum):
    NARROWED = "narrowed"
    FULL_SUITE = "full_suite"


class EnumGraphSource(StrEnum):
    #: No code graph took part in the selection (the only source until the graph lands).
    NONE = "none"


#: Stages of the stage gate: 0 shadow, 1 .. 3 the narrowing rules, 4 the graph.
HIGHEST_STAGE = 4


class ModelStageSelection(BaseModel):
    """One selection: what it runs, why, how long it should take and what produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EnumSelectionKind
    #: Why the whole suite runs; None exactly when the selection is narrowed.
    reason_code: EnumFullSuiteReason | None
    selected_paths: list[TestPath] = Field(min_length=1)
    split_count: int = Field(ge=1, le=40)
    #: Recorded serial seconds the selected paths cover; None when the durations were unread.
    selected_seconds: float | None = Field(ge=0)
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reason_code_only_for_the_full_suite(self) -> Self:
        if (self.kind is EnumSelectionKind.FULL_SUITE) != (
            self.reason_code is not None
        ):
            raise ValueError("reason_code is set exactly when the whole suite runs")
        return self

    @classmethod
    def from_selection(
        cls,
        selection: ModelTestSelection,
        *,
        selected_seconds: float | None,
        selector_version: str,
        policy_version: str,
    ) -> Self:
        return cls(
            kind=(
                EnumSelectionKind.FULL_SUITE
                if selection.is_full_suite
                else EnumSelectionKind.NARROWED
            ),
            reason_code=selection.full_suite_reason,
            selected_paths=selection.selected_paths,
            split_count=selection.split_count,
            selected_seconds=selected_seconds,
            selector_version=selector_version,
            policy_version=policy_version,
        )


def _stages_start_at_zero_without_gaps(stages: Mapping[int, object]) -> None:
    if sorted(stages) != list(range(len(stages))) or len(stages) > HIGHEST_STAGE + 1:
        raise ValueError(
            f"stages must run 0..n without a gap and no higher than {HIGHEST_STAGE}"
        )


class ModelSelectionRecordRequest(BaseModel):
    """What the record is built from: the run's identity and the selection of each stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1)
    #: None for a run that belongs to no pull request (a merge group).
    pr_number: int | None = Field(ge=1)
    head_sha: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    #: The commit the job actually checked out.
    checkout_sha: str = Field(min_length=1)
    run_id: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    changed_files: tuple[str, ...]
    #: True when ENABLE_SMART_TESTS is true.
    flag_enabled: bool
    #: Stage -> the selection the rules at or below that stage give, computed as if the flag were on.
    stage_selections: dict[int, ModelTestSelection] = Field(min_length=1)
    #: The checkout the durations are read from.
    repo_root: str = Field(min_length=1)
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _stages_are_contiguous(self) -> Self:
        _stages_start_at_zero_without_gaps(self.stage_selections)
        return self


class ModelTestSelectionRecord(BaseModel):
    """The selection of one pull request run: what pytest ran and what every stage would run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1)
    pr_number: int | None = Field(ge=1)
    head_sha: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    checkout_sha: str = Field(min_length=1)
    run_id: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    changed_files: tuple[str, ...]
    policy_version: str = Field(min_length=1)
    selection_stage: int = Field(ge=0, le=HIGHEST_STAGE)
    #: What pytest ran: the entry of selection_stage when the flag is on, the full matrix when not.
    executed_selection: ModelStageSelection
    #: Stage -> the selection the rules at or below that stage give. Never what pytest runs.
    selection_by_stage: dict[int, ModelStageSelection] = Field(min_length=1)
    graph_source: EnumGraphSource

    @model_validator(mode="after")
    def _stages_are_contiguous(self) -> Self:
        _stages_start_at_zero_without_gaps(self.selection_by_stage)
        return self
