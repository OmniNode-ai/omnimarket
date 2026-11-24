# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage receipt, and the request and result of validating it against published attribution records."""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    HIGHEST_STAGE,
)


class ModelReceiptNight(BaseModel):
    """One nightly run a receipt covers, named the way the published record names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_date: datetime.date
    nightly_run_id: int = Field(ge=1)
    #: The commit the nightly suite ran on, where the night's record is published as a check run.
    tested_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ModelMeasuredTargets(BaseModel):
    """The targets a stage is measured against. A missing value is refused by the validator, not defaulted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Share of merges whose executed selection was narrowed, 0..1.
    merge_narrowed_share: float | None = Field(default=None, ge=0, le=1)
    #: Recorded test seconds per merge.
    test_seconds_per_merge: float | None = Field(default=None, ge=0)
    #: CI minutes per pull request.
    ci_minutes_per_pr: float | None = Field(default=None, ge=0)


class ModelReceiptComparison(BaseModel):
    """The comparison stage 1 was entered on: its recorded inputs by digest and its figures.

    The validator recomputes every figure from the inputs; none is trusted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    replay_input_path: str = Field(min_length=1)
    replay_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_input_path: str = Field(min_length=1)
    observed_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes_compared: int = Field(ge=0)
    changes_narrowed: int = Field(ge=0)
    runs_with_result: int = Field(ge=0)
    runs_unavailable: int = Field(ge=0)
    failing_tests: int = Field(ge=0)
    failing_tests_selected: int = Field(ge=0)
    misses: int = Field(ge=0)


class ModelStageReceipt(BaseModel):
    """The evidence for entering one stage.

    Stage 0 is the baseline and carries none. Stage 1 is entered on one ``comparison`` over the recorded set and
    names no night; a stage above 1 is entered on ``nights`` of published nightly attribution records.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: int = Field(ge=0, le=HIGHEST_STAGE)
    #: The stage this one is entered from; None only for the baseline.
    entered_from_stage: int | None = Field(default=None, ge=0, le=HIGHEST_STAGE)
    repository: str | None = Field(default=None, min_length=1)
    selector_version: str | None = Field(default=None, min_length=1)
    policy_version: str | None = Field(default=None, min_length=1)
    nights: tuple[ModelReceiptNight, ...] = ()
    comparison: ModelReceiptComparison | None = None
    clean_nights: int = Field(default=0, ge=0)
    pr_record_count: int = Field(default=0, ge=0)
    miss_total: int = Field(default=0, ge=0)
    unattributable_total: int = Field(default=0, ge=0)
    incomplete_total: int = Field(default=0, ge=0)
    #: clean_nights over the nights named, 0..1.
    clean_night_share: float = Field(default=0.0, ge=0, le=1)
    measured: ModelMeasuredTargets | None = None

    @model_validator(mode="after")
    def _baseline_carries_no_evidence(self) -> ModelStageReceipt:
        if self.stage == 0 and (
            self.nights
            or self.comparison
            or self.entered_from_stage is not None
            or self.measured
        ):
            raise ValueError(
                "the stage 0 receipt is the baseline and holds no evidence"
            )
        if self.stage > 0 and self.entered_from_stage is None:
            raise ValueError(
                "a receipt for a stage above 0 names the stage it enters from"
            )
        if self.stage == 1 and self.nights:
            raise ValueError(
                "the stage 1 receipt is entered on a comparison and names no nights"
            )
        if self.stage > 1 and self.comparison is not None:
            raise ValueError(
                "a receipt above stage 1 is entered on nights and records no comparison"
            )
        return self


class EnumStageReceiptViolation(StrEnum):
    RECEIPT_MISSING = "receipt_missing"
    DUPLICATE_RECEIPT = "duplicate_receipt"
    STAGE_JUMP = "stage_jump"
    NIGHTS_SHORT = "nights_short"
    NIGHTS_NOT_CONSECUTIVE = "nights_not_consecutive"
    RECORDS_UNAVAILABLE = "records_unavailable"
    RECORD_MISSING = "record_missing"
    RECORD_MISMATCH = "record_mismatch"
    DUPLICATE_RECORD = "duplicate_record"
    RECORD_WRONG_STAGE_ENTRY = "record_wrong_stage_entry"
    NIGHT_NOT_CLEAN = "night_not_clean"
    VERSION_MISMATCH = "version_mismatch"
    FIGURE_MISMATCH = "figure_mismatch"
    TOO_FEW_PR_RECORDS = "too_few_pr_records"
    TARGETS_ABSENT = "targets_absent"
    RECEIPT_UNIDENTIFIED = "receipt_unidentified"
    COMPARISON_ABSENT = "comparison_absent"
    RECORDED_SET_UNAVAILABLE = "recorded_set_unavailable"
    INPUT_MISMATCH = "input_mismatch"
    COMPARISON_MISS = "comparison_miss"
    COMPARISON_FAILED = "comparison_failed"


class ModelStageReceiptViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: EnumStageReceiptViolation
    #: The stage whose receipt the violation is about.
    stage: int = Field(ge=0, le=HIGHEST_STAGE)
    detail: str = Field(min_length=1)


class ModelStageReceiptValidationRequest(BaseModel):
    """The stage the tree now holds, the stage it held before the change, and every receipt it carries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_stage: int = Field(ge=0, le=HIGHEST_STAGE)
    #: The stage on the base branch; None when it cannot be read, which checks every receipt.
    base_stage: int | None = Field(default=None, ge=0, le=HIGHEST_STAGE)
    receipts: tuple[ModelStageReceipt, ...]


class ModelStageReceiptValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    violations: tuple[ModelStageReceiptViolation, ...]

    @model_validator(mode="after")
    def _valid_means_no_violation(self) -> ModelStageReceiptValidationResult:
        if self.valid == bool(self.violations):
            raise ValueError("valid is true exactly when there is no violation")
        return self
