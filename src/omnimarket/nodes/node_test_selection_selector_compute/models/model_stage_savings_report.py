# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage-savings report: what the selection of each merged pull request saved against the full suite.

One row per merged pull request since the stage went live, read from the pull request's published selection
record and its CI run. A pull request whose record cannot be read, or whose record contradicts the full-suite
figure, is a row with a reason and is left out of every total: it is counted, never averaged in.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumSelectionKind,
)


class EnumSavingsRowStatus(StrEnum):
    READ = "read"
    UNREADABLE = "unreadable"


class EnumSavingsUnreadableReason(StrEnum):
    #: No run of the record's workflow exists for the pull request's merged head.
    NO_RUN = "no_run"
    #: The workflow run is not completed, so its minutes are not final.
    RUN_UNFINISHED = "run_unfinished"
    #: No run of the merged head carries a selection record artifact for the pull request.
    NO_RECORD = "no_record"
    #: The selection record artifact exists but its retention has ended.
    RECORD_EXPIRED = "record_expired"
    #: The gateway refused to hand over the artifact archive (over the size cap, not in the run).
    RECORD_REFUSED = "record_refused"
    #: The archive is not a zip holding one JSON record, or the JSON is not a selection record.
    RECORD_UNPARSEABLE = "record_unparseable"
    #: The record names another repository, pull request, head commit, run or attempt than its artifact.
    RECORD_IDENTITY_MISMATCH = "record_identity_mismatch"
    #: The record's executed selection holds no seconds (the durations were unread when it was built).
    SELECTED_SECONDS_UNREAD = "selected_seconds_unread"
    #: The record's selected seconds exceed the full-suite seconds: the record or the full figure is wrong.
    SELECTED_EXCEEDS_FULL = "selected_exceeds_full"
    #: The gateway gave no answer for one of this pull request's reads.
    GATEWAY_UNREADABLE = "gateway_unreadable"


class ModelStageSavingsReportRequest(BaseModel):
    """Which repository to report on and the checkout whose durations give the full-suite seconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    #: The checkout the full-suite seconds are read from (the policy's durations_path under it).
    repo_root: str = Field(min_length=1)
    #: First pull request of the report; None reads the policy's savings_report_first_pr.
    first_pr_number: int | None = Field(default=None, ge=1)


class ModelPrSavingsRow(BaseModel):
    """One merged pull request: its saving, or why it has none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(ge=1)
    head_sha: str
    status: EnumSavingsRowStatus
    unreadable_reason: EnumSavingsUnreadableReason | None = None
    unreadable_detail: str | None = None
    run_id: int | None = None
    run_attempt: int | None = None
    selection_stage: int | None = None
    executed_kind: EnumSelectionKind | None = None
    #: Seconds the executed selection cost: the record's figure for a narrowed run, the full-suite seconds for a
    #: run of the whole suite (it ran everything, so it saved nothing whatever its estimate said).
    selected_seconds: float | None = Field(default=None, ge=0)
    #: The seconds the record itself holds for its executed selection, before that rule.
    recorded_selected_seconds: float | None = Field(default=None, ge=0)
    full_suite_seconds: float | None = Field(default=None, ge=0)
    seconds_saved: float | None = None
    #: Minutes between the CI run's creation and its last update, the figure the stage comparison uses.
    ci_minutes: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _status_agrees_with_the_fields(self) -> ModelPrSavingsRow:
        read = self.status is EnumSavingsRowStatus.READ
        if read == (self.unreadable_reason is not None):
            raise ValueError(
                "an unreadable reason is set exactly when the row is unreadable"
            )
        if read and (
            self.selected_seconds is None
            or self.full_suite_seconds is None
            or self.seconds_saved is None
            or self.ci_minutes is None
            or self.executed_kind is None
        ):
            raise ValueError("a read row holds every figure")
        if not read and (
            self.selected_seconds is not None
            or self.seconds_saved is not None
            or self.ci_minutes is not None
        ):
            raise ValueError("an unreadable row holds no figure")
        return self


class ModelStageSavingsTotals(BaseModel):
    """Totals over the read rows only; an unreadable row is counted in ``prs_unreadable`` and nowhere else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_in_scope: int = Field(ge=0)
    prs_counted: int = Field(ge=0)
    prs_unreadable: int = Field(ge=0)
    prs_narrowed: int = Field(ge=0)
    #: Counted pull requests by the selection_stage their record was built under (a run that began before the
    #: stage went live holds the older stage), in stage order.
    prs_by_selection_stage: dict[int, int]
    #: Narrowed over counted; None when nothing was counted.
    merge_narrowed_share: float | None = Field(default=None, ge=0, le=1)
    selected_seconds_total: float = Field(ge=0)
    full_suite_seconds_total: float = Field(ge=0)
    seconds_saved_total: float
    #: Saved over the full-suite total; None when nothing was counted.
    seconds_saved_share: float | None = None
    #: Mean selected seconds per counted pull request.
    test_seconds_per_merge: float | None = Field(default=None, ge=0)
    ci_minutes_total: float = Field(ge=0)
    #: Mean CI minutes per counted pull request.
    ci_minutes_per_pr: float | None = Field(default=None, ge=0)
    #: How many unreadable rows each reason holds, in the enum's order, reasons with none left out.
    unreadable_by_reason: dict[EnumSavingsUnreadableReason, int]


class ModelStageSavingsReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    first_pr_number: int = Field(ge=1)
    #: When the first pull request merged; the report holds the pull requests merged at or after it.
    first_pr_merged_at: str
    #: The full-suite test seconds every row's saving is measured against.
    full_suite_seconds: float = Field(ge=0)
    rows: tuple[ModelPrSavingsRow, ...]
    totals: ModelStageSavingsTotals
