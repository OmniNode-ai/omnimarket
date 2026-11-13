# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the goal closure orchestrator (OMN-20033, GC.10).

A closed goal reaches exactly one verification run per goal and contract revision. The run is
keyed on the goal id and the revision; the closing event ids are provenance (model finding F1).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumGoalVerificationState(StrEnum):
    """Where a delivered close is: nowhere is not a state (finding F7)."""

    PENDING = "pending"  # close received, start command not yet published and recorded
    STARTED = "started"  # start command published, no verdict yet
    VERDICT = "verdict"  # a verifier completion event is bound to the goal and revision
    EXHAUSTED = (
        "exhausted"  # the bound is spent with no verdict: a missing-verdict obligation
    )


class EnumGoalClosureSource(StrEnum):
    RESULT = "result"
    DELEGATION_TERMINAL = "delegation_terminal"


class EnumGoalVerificationAction(StrEnum):
    STARTED = "started"
    DUPLICATE_CLOSE = "duplicate_close"
    CONTRACT_UNREAD = "contract_unread"
    DISPOSITION_RECORDED = "disposition_recorded"
    VERDICT_BOUND = "verdict_bound"
    VERDICT_UNMATCHED = "verdict_unmatched"
    RETRIED = "retried"
    EXHAUSTED = "exhausted"
    NO_GOAL = "no_goal"
    WAITING = "waiting"


class ModelGoalClosure(BaseModel):
    """One delivered close of one goal at one contract revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID
    contract_revision: UUID
    closing_event_id: UUID
    source: EnumGoalClosureSource
    observed_at: AwareDatetime


class ModelGoalContractRevision(BaseModel):
    """The goal contract at one revision, as the contract reader returns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID
    revision_id: UUID
    ticket_id: str = Field(..., min_length=1)
    level: Literal["delegate_call", "workflow_lane", "interactive_session"]
    contract_schema_version: str
    dod_evidence: tuple[ModelContractDodItem, ...] = Field(..., min_length=1)


class ModelGoalVerificationRun(BaseModel):
    """The durable record of one run: one per goal id and contract revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID
    contract_revision: UUID
    state: EnumGoalVerificationState
    attempts: int = Field(default=0, ge=0)
    closing_event_ids: tuple[UUID, ...]
    disposition_event_ids: tuple[UUID, ...] = ()
    first_closed_at: datetime
    last_attempt_at: datetime | None = None
    delegation_correlation_id: UUID | None = None
    verdict_correlation_id: UUID | None = None
    obligation_reason: str | None = None

    @property
    def key(self) -> tuple[UUID, UUID]:
        return (self.goal_id, self.contract_revision)


class ModelGoalVerificationDecision(BaseModel):
    """What the orchestrator did for one goal and revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID | None
    contract_revision: UUID | None
    action: EnumGoalVerificationAction
    state: EnumGoalVerificationState | None = None
    attempts: int = 0
    detail: str = ""


class ModelGoalVerificationOutcome(BaseModel):
    """The result of handling one event or command: one decision per goal it touched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[ModelGoalVerificationDecision, ...]


class ModelGoalDispositionRecorded(BaseModel):
    """A disposition event for a goal: recorded, and it never starts a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    goal_id: UUID
    contract_revision: UUID
    disposition: str = Field(..., min_length=1)
    emitted_at: AwareDatetime


class ModelGoalVerificationSweepCommand(BaseModel):
    """Recover runs whose verifier went quiet: retry up to the bound, then write an obligation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime


class ModelUnverifiedClosesQuery(BaseModel):
    """List every delivered close that has no verdict yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID | None = None


class ModelUnverifiedClosesList(BaseModel):
    """Pending, started and exhausted closes, from the moment each close was delivered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[ModelGoalVerificationRun, ...]


class ModelGoalVerificationRules(BaseModel):
    """The orchestrator's rules, read from the contract (an overlay may replace them)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retry_bound: int = Field(..., ge=1, description="Verifier start attempts per run.")
    attempt_timeout_s: int = Field(
        ..., ge=1, description="Quiet seconds before an attempt counts as lost."
    )
    start_command_topic: str
    run_key_namespace: UUID


__all__: list[str] = [
    "EnumGoalClosureSource",
    "EnumGoalVerificationAction",
    "EnumGoalVerificationState",
    "ModelGoalClosure",
    "ModelGoalContractRevision",
    "ModelGoalDispositionRecorded",
    "ModelGoalVerificationDecision",
    "ModelGoalVerificationOutcome",
    "ModelGoalVerificationRules",
    "ModelGoalVerificationRun",
    "ModelGoalVerificationSweepCommand",
    "ModelUnverifiedClosesList",
    "ModelUnverifiedClosesQuery",
]
