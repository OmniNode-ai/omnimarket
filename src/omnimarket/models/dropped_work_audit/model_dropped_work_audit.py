# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dropped-work audit's models (OMN-26205).

The rules are the compute node's overlay: every kind, window, delay, row type, cell name and suggested step is a value
there and none has a default. The request carries the ledger rows, the merged pull requests, the admission refusals
and the items already raised, so the audit itself reads nothing. The audit detects and raises; it assigns no owner and
returns no admission or refusal verdict.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_reading import ModelLedgerEvidenceRow

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumDroppedWorkKind(StrEnum):
    """The six kinds of dropped work."""

    RULING_NO_MECHANISM = "ruling_no_mechanism"
    PROMISE_NO_LANE = "promise_no_lane"
    BLOCKED_NEXT_STEP = "blocked_next_step"
    HOLD_UNRELEASED = "hold_unreleased"
    CLAIM_UNFINISHED = "claim_unfinished"
    ADMISSION_REFUSAL = "admission_refusal"


class ModelDroppedWorkAuditRules(BaseModel):
    """The compute node's overlay (``node_dropped_work_audit_compute``). No key has a default."""

    model_config = _FROZEN

    audit_lane: str = Field(min_length=1)
    kinds: tuple[EnumDroppedWorkKind, ...] = Field(min_length=1)
    lookback_seconds: int = Field(gt=0)
    standing_ruling_lookback_seconds: int = Field(gt=0)
    claim_window_seconds: int = Field(gt=0)
    alarm_delay_seconds: int = Field(ge=0)
    ruling_row_types: tuple[str, ...] = Field(min_length=1)
    promise_row_type: str = Field(min_length=1)
    promise_marker: str = Field(min_length=1)
    claim_row_type: str = Field(min_length=1)
    terminal_row_type: str = Field(min_length=1)
    hold_row_type: str = Field(min_length=1)
    closing_row_types: tuple[str, ...] = Field(min_length=1)
    mechanism_row_types: tuple[str, ...] = Field(min_length=1)
    promise_followup_row_types: tuple[str, ...] = Field(min_length=1)
    activity_row_types: tuple[str, ...] = Field(min_length=1)
    blocked_outcomes: tuple[str, ...] = Field(min_length=1)
    next_step_cells: tuple[str, ...] = Field(min_length=1)
    outcome_cell: str = Field(min_length=1)
    ticket_cell: str = Field(min_length=1)
    pr_cell: str = Field(min_length=1)
    hold_id_cell: str = Field(min_length=1)
    closes_claim_cell: str = Field(min_length=1)
    ignored_ref_cells: tuple[str, ...]
    pr_ref_pattern: str = Field(min_length=1)
    alarm_row_type: str = Field(min_length=1)
    alarm_value: str = Field(min_length=1)
    suggested_steps: dict[EnumDroppedWorkKind, str]

    @field_validator("pr_ref_pattern")
    @classmethod
    def _valid_pattern(cls, value: str) -> str:
        re.compile(value)
        return value

    @model_validator(mode="after")
    def _steps_cover_kinds(self) -> ModelDroppedWorkAuditRules:
        for kind in self.kinds:
            step = self.suggested_steps.get(kind, "")
            if not step.strip():
                raise ValueError(f"suggested_steps has no step for kind {kind.value}")
        for step in self.suggested_steps.values():
            if "|" in step:
                raise ValueError("a suggested step may not contain the row separator")
        return self


class ModelAdmissionRefusal(BaseModel):
    """One refusal of admission, read from the goal scope admission node's typed completed event (``not_admitted``)."""

    model_config = _FROZEN

    candidate: str = Field(min_length=1, description="The candidate keyed repo#n.")
    ticket: str | None = None
    reason: str = ""
    observed_at: datetime


class ModelDroppedWorkAuditRequest(BaseModel):
    """One audit pass over the ledger rows read."""

    model_config = _FROZEN

    now: datetime
    rules: ModelDroppedWorkAuditRules
    rows: tuple[ModelLedgerEvidenceRow, ...]
    merged_prs: dict[str, datetime] = Field(default_factory=dict)
    admission_refusals: tuple[ModelAdmissionRefusal, ...] = ()
    alarmed_items: frozenset[str] = frozenset()
    evented_items: frozenset[str] = frozenset()


class ModelDroppedWorkItem(BaseModel):
    """One item of dropped-work, with a stable id derived from its source row."""

    model_config = _FROZEN

    item_id: str
    kind: EnumDroppedWorkKind
    source_stamp: datetime
    source_lane: str
    ticket: str | None = None
    pr: str | None = None
    unowned_since: datetime
    owned: bool
    owner_reason: str = ""
    suggested_step: str


class ModelOwnerMissingEvent(BaseModel):
    """The typed owner-missing event; its id is the item id, so a consumer deduplicates by it."""

    model_config = _FROZEN

    event_id: str
    kind: EnumDroppedWorkKind
    source_stamp: datetime
    source_lane: str
    ticket: str | None = None
    pr: str | None = None
    unowned_since: datetime
    observed_at: datetime
    suggested_step: str


class ModelDroppedWorkAlarmRow(BaseModel):
    """One alarm row to append, with the request id that makes a redelivery write nothing twice."""

    model_config = _FROZEN

    item_id: str
    request_id: str
    row: str


class ModelDroppedWorkAuditResult(BaseModel):
    """The audit's answer: every item with its owned flag, the counts, and what to raise this pass."""

    model_config = _FROZEN

    now: datetime
    items: tuple[ModelDroppedWorkItem, ...]
    counts_by_kind: dict[str, int]
    unowned: int
    events: tuple[ModelOwnerMissingEvent, ...]
    alarms: tuple[ModelDroppedWorkAlarmRow, ...]


__all__: list[str] = [
    "EnumDroppedWorkKind",
    "ModelAdmissionRefusal",
    "ModelDroppedWorkAlarmRow",
    "ModelDroppedWorkAuditRequest",
    "ModelDroppedWorkAuditResult",
    "ModelDroppedWorkAuditRules",
    "ModelDroppedWorkItem",
    "ModelOwnerMissingEvent",
]
