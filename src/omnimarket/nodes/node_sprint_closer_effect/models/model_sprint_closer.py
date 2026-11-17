# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of the sprint closer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


class EnumCloserVerdict(StrEnum):
    CLOSE = "close"
    LEAVE = "leave"


class EnumCloserAction(StrEnum):
    SET_DONE = "set_done"
    COMMENT_MISSING = "comment_missing"
    COMMENT_EVIDENCE = "comment_evidence"
    NO_CHANGE = "no_change"


class ModelCheckResult(BaseModel):
    """One dod_verify check as the verifier reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    status: EnumCheckStatus
    binds: tuple[str, ...] = ()
    detail: str = ""


class ModelTicketEvidence(BaseModel):
    """Everything the eligibility rule reads about one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    title: str = ""
    state: str = ""
    assignee: str | None = None
    is_collaborator: bool = False
    criteria: tuple[str, ...] = Field(default=(), description="AC labels parsed from the ticket.")
    criteria_ticked: tuple[str, ...] = Field(default=(), description="AC labels whose checkbox is ticked.")
    contract_ref: str | None = Field(default=None, description="repo@sha:path of the contract dod_verify ran.")
    contract_error: str | None = Field(default=None, description="Why dod_verify found or ran no contract.")
    checks: tuple[ModelCheckResult, ...] = ()
    seen_running: tuple[str, ...] = Field(default=(), description="Integration readings naming the change SEEN RUNNING.")


class ModelEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    verdict: EnumCloserVerdict
    basis: str = Field(default="", description="checks | seen_running | empty when left.")
    evidence: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


class ModelTicketDecision(BaseModel):
    """The eligibility verdict plus what the closer does about it, and the comment it writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    eligibility: ModelEligibility
    action: EnumCloserAction
    fingerprint: str
    comment: str | None = None


class ModelTicketOutcome(BaseModel):
    """One ticket activity's result: the decision and what was written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    assignee: str | None = None
    verdict: EnumCloserVerdict
    action: EnumCloserAction
    missing: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    commented: bool = False
    state_set: str | None = None
    dry_run: bool = True


class ModelSprintCloserPlan(BaseModel):
    """The declared settings one workflow run uses (from contract.yaml plus the trigger)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    owner_assignees: tuple[str, ...]
    actor: str
    dry_run: bool = True
    trigger: str = "manual"
    ticket_ids: tuple[str, ...] = Field(default=(), description="Restrict the run to these tickets when set.")
    activity_timeout_seconds: int = 1200
    activity_max_attempts: int = 3
    retry_initial_seconds: int = 10


class ModelSprintCloserRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    trigger: str
    seen_running_marks: tuple[str, ...] = ()
    outcomes: tuple[ModelTicketOutcome, ...] = ()
