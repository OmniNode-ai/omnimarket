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


class EnumPrecheckVerdict(StrEnum):
    ALREADY_DELIVERED = "already-delivered"
    PEER_OWNED = "peer-owned"
    RUN = "run"


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

    @model_validator(mode="after")
    def validate_fields_for_kind(self) -> Self:
        kind = self.kind
        if not DATE_PATTERN.match(self.date):
            raise ValueError(f"date must be YYYY-MM-DD, got {self.date!r}")
        required: dict[EnumCloseoutDecisionKind, tuple[str, ...]] = {
            EnumCloseoutDecisionKind.PRECHECK: ("clock_utc", "ledger_rows"),
            EnumCloseoutDecisionKind.RESOLVE_SPRINT: ("projects",),
            EnumCloseoutDecisionKind.PLAN_SCOPE: ("project_id", "tickets"),
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
            if getattr(self, field) != defaults[field].default:
                raise ValueError(f"{kind.value} does not take {field}")
        if kind is EnumCloseoutDecisionKind.PRECHECK:
            assert self.clock_utc is not None
            try:
                datetime.strptime(self.clock_utc, CLOCK_FORMAT)
            except ValueError as exc:
                raise ValueError(
                    f"clock_utc must be {CLOCK_FORMAT}, got {self.clock_utc!r}"
                ) from exc
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
