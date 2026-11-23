# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, verdict, event and overlay of node_plan_owner_check_effect."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    ModelReaderResult,
)

_FINDING = r"^[a-z][a-z0-9-]{0,63}$"
# A pull request of the plan: ``<owner/repo>#<n>`` or ``<repo>#<n>``.
_PR = r"^([A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#\d+$"


class EnumPlanOwnerVerdict(StrEnum):
    """OWNED: a live claim names the plan. MISSING: a fresh read answered and none does. UNKNOWN: no read answered."""

    OWNED = "OWNED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ModelPlanOwnerRule(BaseModel):
    """One plan to watch: the finding it answers, what a claim must name to own it, the live window.

    A live claim owns the plan when its ticket is listed, its plan cell names a listed plan path,
    or its ``pr=`` names a listed pull request. The lane's name never decides: a rule that lists
    none of the three is refused at load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: str = Field(pattern=_FINDING)
    # Never read. The overlays repository's main branch still carries this key on the live rule,
    # and CI loads every overlays-main document through this model before that rule can change,
    # so the key stays accepted until the overlays rule drops it; a follow-up then removes the
    # field. It decides nothing: ownership is by ticket, plan path and pull request only.
    lane_prefixes: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    # Plan paths as the claim's plan cell names them; compared by file stem (directories and a
    # ``.md`` suffix do not matter), so ``plans/<name>.md`` and ``<name>`` are one plan.
    plan_paths: tuple[str, ...] = ()
    # Pull requests of the plan, ``<owner/repo>#<n>`` or ``<repo>#<n>``; compared by ``<repo>#<n>``.
    prs: tuple[Annotated[str, Field(pattern=_PR)], ...] = ()
    # How recent a claim's newest row must be for the claim to count as live.
    live_window_minutes: int = Field(ge=1)
    why: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    # What to do when the read failed or its answer cannot be read (the verdict is UNKNOWN).
    unknown_fix: str = Field(min_length=1)

    @model_validator(mode="after")
    def _names_something(self) -> ModelPlanOwnerRule:
        """A rule that names no ticket, plan path or pull request could own nothing; refuse."""
        if not (self.tickets or self.plan_paths or self.prs):
            raise ValueError(
                f"rule {self.finding!r} names no ticket, plan path or pull request"
            )
        return self


class ModelPlanOwnerOverlay(BaseModel):
    """Every deployment fact of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The reader name this node reads the open-claims answer under.
    reader: str = Field(pattern=_FINDING)
    # The reader asks only for claims opened within this many hours; the query node refuses
    # an unwindowed open-claims answer over its byte bound.
    claims_window_hours: int = Field(ge=1, le=24 * 30)
    rules: tuple[ModelPlanOwnerRule, ...] = Field(min_length=1)
    # The environment variable naming the file the node's events are journalled to.
    journal_env: str = Field(min_length=1)

    @model_validator(mode="after")
    def _findings_unique(self) -> ModelPlanOwnerOverlay:
        """Two rules answering one finding would print two verdicts for it; refuse."""
        findings = [rule.finding for rule in self.rules]
        if len(set(findings)) != len(findings):
            raise ValueError("rule findings must be unique")
        return self

    @model_validator(mode="after")
    def _window_covers_rules(self) -> ModelPlanOwnerOverlay:
        """The read sends only the claims opened inside the claims window, so a rule whose
        live window outlasts it could never see its owner; refuse."""
        if any(
            rule.live_window_minutes > self.claims_window_hours * 60
            for rule in self.rules
        ):
            raise ValueError(
                "claims_window_hours must cover every rule's live_window_minutes"
            )
        return self


class ModelPlanOwnerRequest(BaseModel):
    """One finding to decide and the reader's answer for ``open-claims``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: str
    node_answer: ModelReaderResult


class ModelPlanOwnerEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReaderEventKind
    topic: str
    finding: str
    verdict: EnumPlanOwnerVerdict
    reason: str
    at: AwareDatetime
    owners: tuple[str, ...] = ()


class ModelPlanOwnerVerdict(BaseModel):
    """The verdict the hourly throughput tick prints; MISSING never follows a failed or stale read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: str
    verdict: EnumPlanOwnerVerdict
    reason: str
    owners: tuple[str, ...] = ()
    why: str = ""
    fix: str = ""
    event: ModelPlanOwnerEvent | None = None

    def line(self) -> str:
        """The line the tick prints; MISSING carries the rule's why and its fix."""
        if self.verdict is EnumPlanOwnerVerdict.OWNED:
            return f"OWNED {self.finding} | {','.join(self.owners)}"
        if self.verdict is EnumPlanOwnerVerdict.MISSING:
            return f"MISSING {self.finding} | {self.why} | FIX: {self.fix}"
        line = f"UNKNOWN {self.finding} | {self.reason}"
        return f"{line} | FIX: {self.fix}" if self.fix else line

    def log_line(self) -> str:
        """One machine-readable line; the owners are the lanes joined by comma."""
        owners = ",".join(self.owners) or "none"
        return (
            f"PLAN-OWNER finding={self.finding} verdict={self.verdict.value} "
            f"reason={self.reason} owners={owners}"
        )
