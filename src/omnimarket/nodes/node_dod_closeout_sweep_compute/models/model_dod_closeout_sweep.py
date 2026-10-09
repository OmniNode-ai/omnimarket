# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the DoD closeout sweep decisions (OMN-20675)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLOCK_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class EnumCloseoutDecisionKind(StrEnum):
    PRECHECK = "precheck"
    RESOLVE_SPRINT = "resolve_sprint"
    PLAN_SCOPE = "plan_scope"
    FLIP_DECISION = "flip_decision"
    REPORT = "report"


class EnumPrecheckVerdict(StrEnum):
    ALREADY_DELIVERED = "already-delivered"
    PEER_OWNED = "peer-owned"
    RUN = "run"


class EnumCloseoutAction(StrEnum):
    FLIPPED_DONE = "flipped-done"
    HELD_GAP = "held-gap"
    HELD_MERGED_UNRELEASED = "held-merged-unreleased"
    HELD_EXTERNAL = "held-external"
    CORRECTED_STATE = "corrected-state"
    NO_CHANGE = "no-change"


class EnumReleasedState(StrEnum):
    """What the released check said about the merges a ticket cites."""

    RELEASED = "released"
    MERGED_UNRELEASED = "merged-unreleased"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not-applicable"


class ModelSprintProject(BaseModel):
    """One Linear project as the caller listed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Short identifier, not the uuid.")
    uuid: str = Field(min_length=1)
    name: str
    start_date: str | None = None
    target_date: str | None = None
    completed_at: str | None = None


class ModelScopeTicket(BaseModel):
    """One started ticket with the facts the caller read for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    title: str = ""
    state_type: str = Field(min_length=1)
    state_name: str = ""
    assignee: str | None = None
    external: bool = False
    has_children: bool = False
    fenced: bool = False
    fence_reason: str = ""
    occ_contract: bool = False
    merged_pr_count: int = Field(default=0, ge=0)


class ModelScopeCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enumerated: int
    in_progress: int
    in_review: int
    fenced: int
    parents: int
    external: int
    no_contract_no_merged_pr: int
    occ_contract_present: int
    candidates: int
    candidates_with_occ_contract: int


class ModelNonCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    why_not: str


class ModelTicketResult(BaseModel):
    """One ticket's disposition as a verify chunk reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    action: EnumCloseoutAction
    verdict: str = ""
    evidence: str = ""
    product_pr: str = ""
    merge_sha: str = ""
    receipt: str = ""
    unmet_check: str = ""
    primary_blocker: str = ""
    tooling_blockers: list[str] = Field(default_factory=list)
    behavior_proving_count: int | None = Field(default=None, ge=0)
    predicate_met: bool | None = None
    repo: str = ""
    tag_lookup: str = ""
    index_read: str = ""
    release_ticket: str = ""


class ModelChunkResult(BaseModel):
    """One chunk: the tickets it was given, its verify results and its audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk: int = Field(ge=0)
    tickets: list[str] = Field(min_length=1)
    dropped: bool = False
    results: list[ModelTicketResult] = Field(default_factory=list)
    audited: bool = False
    reverted: list[str] = Field(default_factory=list)
    audit_notes: str = ""


class ModelHistogramRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_class: str
    count: int


class ModelCloseoutCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enumerated: int
    candidates: int
    flipped: int
    held: int
    merged_unreleased: int
    corrected: int
    reverted: int
    fenced: int
    parents: int
    external: int


class ModelDodCloseoutDecisionRequest(BaseModel):
    """One decision of the closeout sweep; the caller performs every read and passes what it saw."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumCloseoutDecisionKind
    date: str
    # precheck
    force: bool = False
    clock_utc: str | None = None
    ledger_rows: list[str] | None = None
    report_text: str | None = None
    report_commit_sha: str = ""
    report_dirty: bool = False
    # resolve_sprint
    project_override: str = ""
    projects: list[ModelSprintProject] | None = None
    # plan_scope
    project_id: str | None = None
    tickets: list[ModelScopeTicket] | None = None
    chunk_size: int = Field(default=5, ge=1)
    # flip_decision
    ticket_id: str | None = Field(default=None, min_length=1)
    total_checks: int | None = Field(default=None, ge=0)
    verified_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    non_probative_count: int = Field(default=0, ge=0)
    behavior_proving_count: int = Field(default=0, ge=0)
    criteria: list[str] = Field(default_factory=list)
    bound_criteria: list[str] = Field(default_factory=list)
    prior_reversal: bool = False
    outcome_changed_since_reversal: bool = False
    all_prs_merged: bool | None = None
    released: EnumReleasedState = EnumReleasedState.NOT_APPLICABLE
    # report
    project_name: str = ""
    scope_counts: ModelScopeCounts | None = None
    chunk_results: list[ModelChunkResult] | None = None
    apply: bool = True
    plan_comparison: str = ""
    not_done: str = ""
    friction: str = "none"
    released_probe_run: bool = True
    released_positive_control: str = ""

    @model_validator(mode="after")
    def validate_fields_for_kind(self) -> Self:
        kind = self.kind
        if not DATE_PATTERN.match(self.date):
            raise ValueError(f"date must be YYYY-MM-DD, got {self.date!r}")
        required: dict[EnumCloseoutDecisionKind, tuple[str, ...]] = {
            EnumCloseoutDecisionKind.PRECHECK: ("clock_utc", "ledger_rows"),
            EnumCloseoutDecisionKind.RESOLVE_SPRINT: ("projects",),
            EnumCloseoutDecisionKind.PLAN_SCOPE: ("project_id", "tickets"),
            EnumCloseoutDecisionKind.FLIP_DECISION: (
                "ticket_id",
                "total_checks",
                "all_prs_merged",
            ),
            EnumCloseoutDecisionKind.REPORT: (
                "project_id",
                "scope_counts",
                "chunk_results",
            ),
        }
        optional: dict[EnumCloseoutDecisionKind, tuple[str, ...]] = {
            EnumCloseoutDecisionKind.PRECHECK: (
                "force",
                "report_text",
                "report_commit_sha",
                "report_dirty",
            ),
            EnumCloseoutDecisionKind.RESOLVE_SPRINT: ("project_override",),
            EnumCloseoutDecisionKind.PLAN_SCOPE: ("chunk_size",),
            EnumCloseoutDecisionKind.FLIP_DECISION: (
                "verified_count",
                "failed_count",
                "non_probative_count",
                "behavior_proving_count",
                "criteria",
                "bound_criteria",
                "prior_reversal",
                "outcome_changed_since_reversal",
                "released",
            ),
            EnumCloseoutDecisionKind.REPORT: (
                "project_name",
                "apply",
                "plan_comparison",
                "not_done",
                "friction",
                "released_probe_run",
                "released_positive_control",
            ),
        }
        for field in required[kind]:
            if getattr(self, field) is None:
                raise ValueError(f"{kind.value} requires {field}")
        owned = {
            name for names in (*required.values(), *optional.values()) for name in names
        }
        allowed = set(required[kind]) | set(optional[kind])
        defaults = type(self).model_fields
        for field in sorted(owned - allowed):
            if getattr(self, field) != defaults[field].get_default(
                call_default_factory=True
            ):
                raise ValueError(f"{kind.value} does not take {field}")
        if kind is EnumCloseoutDecisionKind.PRECHECK:
            assert self.clock_utc is not None
            try:
                datetime.strptime(self.clock_utc, CLOCK_FORMAT)
            except ValueError as exc:
                raise ValueError(
                    f"clock_utc must be {CLOCK_FORMAT}, got {self.clock_utc!r}"
                ) from exc
        if kind is EnumCloseoutDecisionKind.REPORT:
            if "|" in self.friction or "\n" in self.friction:
                raise ValueError("friction is one ledger cell: no pipe and no newline")
            assert self.chunk_results is not None
            result_ids = [r.id for c in self.chunk_results for r in c.results]
            if len(set(result_ids)) != len(result_ids):
                raise ValueError("report got a ticket in more than one result")
            chunk_numbers = [c.chunk for c in self.chunk_results]
            if len(set(chunk_numbers)) != len(chunk_numbers):
                raise ValueError("report got a chunk number twice")
        return self


class ModelDodCloseoutDecisionResult(BaseModel):
    """What to do next; no field is set that the kind did not decide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumCloseoutDecisionKind
    # precheck
    verdict: EnumPrecheckVerdict | None = None
    evidence: str | None = None
    reason: str | None = None
    report_lines: int | None = None
    checks_failed: list[str] | None = None
    # resolve_sprint
    resolved: bool | None = None
    project_id: str | None = None
    project_name: str | None = None
    residuals: list[str] | None = None
    # plan_scope
    counts: ModelScopeCounts | None = None
    candidates: list[str] | None = None
    chunks: list[list[str]] | None = None
    non_candidates: list[ModelNonCandidate] | None = None
    overlaps: list[str] | None = None
    verified_nothing: bool | None = None
    # flip_decision
    action: EnumCloseoutAction | None = None
    predicate_met: bool | None = None
    unmet: list[str] | None = None
    primary_blocker: str | None = None
    unbound_criteria: list[str] | None = None
    # report
    report_path: str | None = None
    report_text: str | None = None
    closeout_counts: ModelCloseoutCounts | None = None
    primary_histogram: list[ModelHistogramRow] | None = None
    tooling_histogram: list[ModelHistogramRow] | None = None
    behavior_proving_positive: int | None = None
    predicate_satisfied: int | None = None
    dropped_chunks: list[int] | None = None
    unadjudicated_tickets: list[str] | None = None
    terminal_cells: list[str] | None = None
