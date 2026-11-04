# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The machinery tier of the lab-fill plan: its policy, facts and candidates (OMN-22413).

Machinery is the work that fixes the factory itself: a FRICTION class that keeps recurring in the
rolling ledger, a throughput-tick key that stays MISSING or UNKNOWN across ticks, and an open ticket
whose description carries a ``Class: <ticket_class>`` line. The planner reads no file or network:
the fill effect hands it the ledger rows, tick lines, tickets and live CLAIMs as facts. The window,
the recurrence floors, the tier order and the priority class are the policy, held in the node's
contract under ``config.lab_fill_plan.machinery`` and carrying no default here.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow

_FROZEN = ConfigDict(frozen=True, extra="forbid")
SOURCES = ("friction-class", "tick-key", "machinery-ticket")
MACHINERY_KIND = "machinery"


class ModelLabFillMachineryPolicy(BaseModel):
    """How the machinery tier is built and where it ranks; every value comes from the contract."""

    model_config = _FROZEN

    priority_class: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    ticket_class: str = Field(min_length=1)
    window_hours: int = Field(ge=1)
    friction_recurrence_floor: int = Field(ge=1)
    tick_recurrence_floor: int = Field(ge=1)
    tick_statuses: tuple[str, ...] = Field(min_length=1)
    source_order: tuple[str, ...] = Field(min_length=1)
    tier_order: tuple[tuple[str, ...], ...] = Field(min_length=1)

    @field_validator("source_order")
    @classmethod
    def _sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not set(value) <= set(SOURCES):
            raise ValueError(f"source_order must name each of {SOURCES} at most once")
        return value

    @model_validator(mode="after")
    def _tiers(self) -> Self:
        kinds = [k for group in self.tier_order for k in group]
        if not all(self.tier_order) or len(set(kinds)) != len(kinds):
            raise ValueError("tier_order groups must be non-empty and name a kind once")
        if MACHINERY_KIND not in kinds:
            raise ValueError(f"tier_order must rank the {MACHINERY_KIND} kind")
        return self


class ModelLabFillMachineryFrictionRow(BaseModel):
    """One FRICTION row of the rolling ledger window, as the fill effect parsed it."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    ts: str
    lane: str = ""
    class_name: str = Field(default="", alias="class")
    guard: str = ""
    reason: str = ""
    ticket: str = ""
    existing: str = ""


class ModelLabFillMachineryTickLine(BaseModel):
    """One key of one throughput-tick log line and the status the tick printed for it."""

    model_config = _FROZEN

    tick: str
    key: str
    status: str


class ModelLabFillMachineryTicket(BaseModel):
    """What Linear holds for a ticket whose description may carry the machinery class line."""

    model_config = _FROZEN

    identifier: str
    title: str = ""
    description: str = ""
    state_type: str = ""
    completed_at: str = ""
    has_children: bool = False
    updated_at: str = ""
    labels: tuple[str, ...] = ()
    project_id: str = ""
    assignee_id: str = ""


class ModelLabFillMachineryClaim(BaseModel):
    """One live CLAIM row of the ledger: its lane, its ticket and its row text."""

    model_config = _FROZEN

    lane: str
    ticket: str = ""
    text: str = ""


class ModelLabFillMachineryCandidatesRequest(BaseModel):
    """The facts the fill effect read; a policy left out is the contract's."""

    model_config = _FROZEN

    observed_at: str
    friction_rows: tuple[ModelLabFillMachineryFrictionRow, ...] = ()
    tick_lines: tuple[ModelLabFillMachineryTickLine, ...] = ()
    tickets: tuple[ModelLabFillMachineryTicket, ...] = ()
    claims: tuple[ModelLabFillMachineryClaim, ...] = ()
    # HOLD and RELEASE rows: a deploy-drift tick key whose daemon a live scope=retired-units HOLD
    # names is skipped as held, so the held repair is not dispatched again.
    hold_rows: tuple[ModelLandingLedgerRow, ...] = ()
    policy: ModelLabFillMachineryPolicy | None = None


class ModelLabFillMachineryCandidate(BaseModel):
    """A machinery candidate in the shape the dispatch plan reads."""

    model_config = _FROZEN

    kind: str = MACHINERY_KIND
    source: str
    machinery_key: str
    ticket: str = ""
    title: str
    evidence: str
    priority_class: str
    labels: tuple[str, ...] = ()
    area: str = ""
    milestone: str = ""
    under_parent: bool = False
    updated_at: str = ""
    project_id: str = ""
    assignee_id: str = ""
    ticket_prs: tuple[str, ...] = ()
    declared_repo: str = ""
    self_contained_code: bool = False


class ModelLabFillMachineryDiagnostic(BaseModel):
    """The tier's read as the idle row reports it: groups seen, candidates eligible, drops by reason."""

    model_config = _FROZEN

    read: str = "ok"
    seen: int = 0
    eligible: int = 0
    # "<reason>:<n>,..." most frequent first: the idle row prints this cell as it stands.
    reasons: str = ""


class ModelLabFillMachinerySkip(BaseModel):
    """A machinery class not offered, and why (``owned:<lane>`` when a live CLAIM names it)."""

    model_config = _FROZEN

    id: str
    reason: str
    kind: str = MACHINERY_KIND


class ModelLabFillMachineryCandidatesResult(BaseModel):
    """The candidates in tier order, the classes skipped and the tier's diagnostic."""

    model_config = _FROZEN

    candidates: tuple[ModelLabFillMachineryCandidate, ...]
    skipped: tuple[ModelLabFillMachinerySkip, ...]
    diagnostic: ModelLabFillMachineryDiagnostic
