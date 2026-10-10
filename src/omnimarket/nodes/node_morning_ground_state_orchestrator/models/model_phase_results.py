# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed versions of the original phase schemas; no schema coercion."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelIdempotencyPrecheckResultPhasesItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    phase: Literal[
        "GroundState", "Triage", "Reconcile", "Integrate", "DroppedWork", "Goal"
    ]
    artifact: str
    verdict: Literal["already-delivered", "peer-owned", "run"]
    evidence: str
    reason: str
    lines: int | None = None
    checks_failed: list[str] | None = None


class ModelIdempotencyPrecheckResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    date: str
    clock_utc: str
    phases: list[ModelIdempotencyPrecheckResultPhasesItem]
    residuals: list[str]


class ModelDecisionsRegisterResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    status: Literal["clean", "alarm", "blocked"]
    total: int
    counts: str
    stale: int
    asked: int
    oldest_asked: str
    oldest_pending: str
    commit_sha: str
    notes: str


class ModelGroundStateResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelGroundStateResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    state: str
    detail: str
    prs: list[str]
    residuals: list[str]
    delegation: ModelGroundStateResultDelegation


class ModelMorningTriageResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelMorningTriageResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    state: str
    detail: str
    prs: list[str]
    residuals: list[str]
    delegation: ModelMorningTriageResultDelegation


class ModelPlanReconcileResultRowsItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: str
    source: str
    title: str
    verdict: Literal["OPEN", "LANDED", "DEPLOYED", "PROBED", "UNKNOWN"]
    evidence: str
    probe: str
    beta_path: bool
    ticket: str | None = None


class ModelPlanReconcileResultCounts(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    total: int
    open: int
    landed: int
    deployed: int
    probed: int
    unknown: int


class ModelPlanReconcileResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelPlanReconcileResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    report_path: str
    sources_read: list[str]
    rows: list[ModelPlanReconcileResultRowsItem]
    counts: ModelPlanReconcileResultCounts
    top_three: list[str]
    linear_mismatches: list[str]
    residuals: list[str]
    delegation: ModelPlanReconcileResultDelegation


class ModelIntegrationPlanResultReposScannedItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    repo: str
    merged_in_window: int
    row_ids: list[str]
    note: str


class ModelIntegrationPlanResultRowsItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: str
    what: str
    source: str
    kind: Literal["deploy", "repin", "wire", "release", "dogfood"]
    destination: str
    next_action: str
    probe: str
    hold: str
    ticket: str
    ticket_created: bool | None = None
    unblocks: str


class ModelIntegrationPlanResultCounts(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    total: int
    from_landed: int
    from_window_merges: int
    held: int
    actionable: int


class ModelIntegrationPlanResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelIntegrationPlanResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    report_path: str
    window: str
    repos_scanned: list[ModelIntegrationPlanResultReposScannedItem]
    rows: list[ModelIntegrationPlanResultRowsItem]
    counts: ModelIntegrationPlanResultCounts
    holds: list[str]
    tickets_created: list[str]
    residuals: list[str]
    delegation: ModelIntegrationPlanResultDelegation


class ModelDroppedWorkResultSectionsItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    section: Literal[
        "RED ON DEV HEAD",
        "STALE PLANS",
        "UNSTARTED WORK BY AGE",
        "UNREAD LANE REPORTS",
        "PROPOSED DISPATCH LIST",
    ]
    count: int
    probe: str
    positive_control: str
    rows: list[str]
    unknown: list[str] | None = None


class ModelDroppedWorkResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelDroppedWorkResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    report_path: str
    sections: list[ModelDroppedWorkResultSectionsItem]
    headline: list[str]
    residuals: list[str]
    delegation: ModelDroppedWorkResultDelegation


class ModelSessionGoalResultDelegation(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    delegated: int = Field(..., ge=0)
    runs: list[str]
    reason: str


class ModelSessionGoalResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    goal_path: str
    goal_rows: list[str]
    integration_subrows: list[str]
    publish_state: Literal["PUBLISHED", "SKIPPED", "BLOCKED"]
    kb_pr: str
    linear_corrected: list[str]
    linear_flagged: list[str]
    residuals: list[str]
    delegation: ModelSessionGoalResultDelegation
