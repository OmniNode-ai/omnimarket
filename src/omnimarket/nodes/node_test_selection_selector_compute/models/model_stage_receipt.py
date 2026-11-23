# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage receipt, and the request and result of validating it against a recomputed comparison."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    HIGHEST_STAGE,
)


class ModelMeasuredTargets(BaseModel):
    """The targets a stage is measured against. A missing value is refused by the validator, not defaulted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Share of the compared changes whose selection is narrowed, 0..1.
    merge_narrowed_share: float | None = Field(default=None, ge=0, le=1)
    #: Recorded test seconds of the selections, per compared change.
    test_seconds_per_merge: float | None = Field(default=None, ge=0)
    #: Mean wall minutes of the recorded full runs (run start to last update), per pull request.
    ci_minutes_per_pr: float | None = Field(default=None, ge=0)


class ModelReceiptComparison(BaseModel):
    """The comparison a stage was entered on: its recorded inputs by digest and its figures.

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
    """The evidence for entering one stage. Stage 0 is the baseline and carries no comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: int = Field(ge=0, le=HIGHEST_STAGE)
    #: The stage this one is entered from; None only for the baseline.
    entered_from_stage: int | None = Field(default=None, ge=0, le=HIGHEST_STAGE)
    repository: str | None = Field(default=None, min_length=1)
    #: The selector node version the comparison was recomputed with.
    selector_version: str | None = Field(default=None, min_length=1)
    comparison: ModelReceiptComparison | None = None
    measured: ModelMeasuredTargets | None = None

    @model_validator(mode="after")
    def _baseline_carries_no_evidence(self) -> ModelStageReceipt:
        if self.stage == 0 and (
            self.comparison or self.entered_from_stage is not None or self.measured
        ):
            raise ValueError(
                "the stage 0 receipt is the baseline and holds no evidence"
            )
        if self.stage > 0 and self.entered_from_stage is None:
            raise ValueError(
                "a receipt for a stage above 0 names the stage it enters from"
            )
        return self


class EnumStageReceiptViolation(StrEnum):
    RECEIPT_MISSING = "receipt_missing"
    DUPLICATE_RECEIPT = "duplicate_receipt"
    STAGE_JUMP = "stage_jump"
    RECEIPT_UNIDENTIFIED = "receipt_unidentified"
    COMPARISON_ABSENT = "comparison_absent"
    RECORDED_SET_UNAVAILABLE = "recorded_set_unavailable"
    INPUT_MISMATCH = "input_mismatch"
    VERSION_MISMATCH = "version_mismatch"
    FIGURE_MISMATCH = "figure_mismatch"
    COMPARISON_FAILED = "comparison_failed"
    TARGETS_ABSENT = "targets_absent"


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
