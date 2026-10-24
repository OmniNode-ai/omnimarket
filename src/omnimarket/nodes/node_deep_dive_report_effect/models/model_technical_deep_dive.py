# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen command, claim and result models for the nightly technical report."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumReportSection(StrEnum):
    """The five claim sections of a technical deep dive."""

    CHANGED_BY_AREA = "changed_by_area"
    CAUSES_AND_FIXES = "causes_and_fixes"
    DECISIONS = "decisions"
    OPEN_BLOCKERS = "open_blockers"
    DO_NOT_REDISCOVER = "do_not_rediscover"


class ModelLedgerRef(BaseModel):
    """A timestamp and lane identifying a work-ledger row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    lane: str = Field(min_length=1)


class ModelClaimCitation(BaseModel):
    """Validated evidence for a claim; an empty citation is refused at render."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    pr: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+#\d+$")
    ledger: ModelLedgerRef | None = None

    @property
    def is_cited(self) -> bool:
        """Whether at least one evidence reference is present."""
        return any(
            value is not None for value in (self.commit_sha, self.pr, self.ledger)
        )

    def render(self) -> str:
        """Render evidence in stable PR, commit, ledger order."""
        parts: list[str] = []
        if self.pr is not None:
            parts.append(f"`{self.pr}`")
        if self.commit_sha is not None:
            parts.append(f"`{self.commit_sha}`")
        if self.ledger is not None:
            parts.append(f"ledger `{self.ledger.ts}` lane `{self.ledger.lane}`")
        return " · ".join(parts)


class ModelReportClaim(BaseModel):
    """A section-scoped statement and the evidence that supports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    section: EnumReportSection
    area: str
    text: str = Field(min_length=1)
    citation: ModelClaimCitation


class ModelTechnicalDeepDiveCommand(BaseModel):
    """JSON source data and ledger rows supplied to the pure report handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    report_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    owner_summary_facts: dict[str, Any]
    dropped_decisions: dict[str, Any] | None = None
    ledger_rows: tuple[str, ...] = ()
    redact_terms: tuple[str, ...] = ()


class ModelRefusedClaim(BaseModel):
    """An uncited claim and its refusal reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: ModelReportClaim
    reason: str


class ModelTechnicalDeepDiveResult(BaseModel):
    """Rendered report with its accepted claims and source accounting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    report_date: str
    report_markdown: str
    claims: tuple[ModelReportClaim, ...]
    uncited_source_items: int
    decisions_source: Literal["dropped-decisions.v1", "ledger-rulings"]
    counts: dict[str, int]
