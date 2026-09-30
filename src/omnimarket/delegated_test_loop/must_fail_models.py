# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed records of the must-fail control on a test-run evidence item (OMN-20032).

A ``test_passes`` check that exits 0 says the tests pass at the head. It does
not say the tests would have failed before the change, and a test that passes
both with and without the change asserts nothing about the change. The control
re-runs the PR's changed tests against the code as it was before the PR and
records what happened, so a receipt distinguishes a proof from a coincidence.

The grade itself is not computed here. It is ``grade_control`` of
``node_delegated_test_control_compute`` (OMN-19361), the same compute the
delegated test loop uses; this module only holds the record a receipt carries.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Longest reason text a receipt carries.
MAX_REASON_CHARS = 400


class EnumMustFailControlOutcome(StrEnum):
    """What the control established about a passing test run."""

    #: A changed test failed at assertion level before the change (headline).
    CONTROLLED = "controlled"
    #: A changed test failed only at collection or setup before the change. The
    #: ruling of 2026-09-23 counts it as accepted-weak, reported apart from the
    #: headline.
    CONTROLLED_WEAK = "controlled_weak"
    #: Every changed test passed before the change too: the run does not
    #: discriminate, so its green is not evidence about the change.
    VACUOUS = "vacuous"
    #: A control cannot exist for this item. The reason is named, never implied.
    IMPOSSIBLE = "impossible"
    #: A control should exist and could not be run. The item is not proven.
    UNAVAILABLE = "unavailable"


class EnumMustFailImpossibleReason(StrEnum):
    """Why a must-fail control cannot exist. One explicit reason per case."""

    DOCS_ONLY_DIFF = "docs_only_diff"
    NO_TEST_FILE_IN_DIFF = "no_test_file_in_diff"
    #: Tests changed and nothing else did, so there is no pre-change behaviour
    #: for the tests to fail against.
    TEST_ONLY_DIFF = "test_only_diff"
    NO_PRE_CHANGE_COMMIT = "no_pre_change_commit"
    #: The item's own description declares a pure refactor or a docs change.
    DECLARED_BY_ITEM = "declared_by_item"
    #: The item's command is not a list of pytest files, so there is nothing to
    #: re-run against the earlier code.
    COMMAND_NOT_A_TEST_FILE_LIST = "command_not_a_test_file_list"
    #: The item is a ``test_passes`` check that is not derived from a PR diff,
    #: so no PR names the changed tests. Recorded, never graded.
    NOT_DIFF_DERIVED = "not_diff_derived"


class ModelMustFailTestRun(BaseModel):
    """One changed test file, run against the pre-change code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    outcome: str = Field(
        ...,
        description="The pytest failure digest's outcome for this file: passed, "
        "failed_call, failed_collection, error_setup, no_tests or infra_error.",
    )


class ModelPrChangedFile(BaseModel):
    """One file a merged PR changed, as GitHub's pull-request files list says."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: str = Field(
        ...,
        description="GitHub's per-file status: added, modified, removed, renamed, "
        "copied, changed or unchanged.",
    )


class ModelPrDiffFacts(BaseModel):
    """What the control needs to know about the PR it re-runs the tests of."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr_number: int
    merge_commit_sha: str = Field(
        default="", description="Empty when the PR is not merged."
    )
    parent_commit_sha: str = Field(
        default="",
        description="The first parent of the merge commit: the code as it was "
        "before the PR. Empty for a root commit or an unmerged PR.",
    )
    changed_files: tuple[ModelPrChangedFile, ...] = ()


class ModelMustFailControl(BaseModel):
    """The control's record on one evidence check result.

    ``route`` says which route the item took. ``control`` re-ran the changed
    tests at the pre-change code and graded them. ``shell`` ran the command
    only, so the result is never labelled controlled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["control", "shell"]
    outcome: EnumMustFailControlOutcome
    headline: bool = Field(
        default=False,
        description="True only for an assertion-level failure before the change.",
    )
    grade_status: str = Field(
        default="",
        description="The status grade_control returned; empty when it did not run.",
    )
    impossible_reason: EnumMustFailImpossibleReason | None = None
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)
    repo: str = ""
    pr_number: int | None = None
    pre_change_sha: str = ""
    change_sha: str = ""
    runs: tuple[ModelMustFailTestRun, ...] = ()


__all__ = [
    "MAX_REASON_CHARS",
    "EnumMustFailControlOutcome",
    "EnumMustFailImpossibleReason",
    "ModelMustFailControl",
    "ModelMustFailTestRun",
    "ModelPrChangedFile",
    "ModelPrDiffFacts",
]
