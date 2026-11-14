# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, verdict, event and overlay of node_plan_owner_check_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    ModelReaderResult,
)

_FINDING = r"^[a-z][a-z0-9-]{0,63}$"


class EnumPlanOwnerVerdict(StrEnum):
    """OWNED: a live claim names the plan. MISSING: a fresh read answered and none does. UNKNOWN: no read answered."""

    OWNED = "OWNED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ModelPlanOwnerRule(BaseModel):
    """One plan to watch: the finding it answers, the claims that may own it, the live window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: str = Field(pattern=_FINDING)
    # A claim owns the plan when its lane starts with any prefix, or its ticket is listed.
    lane_prefixes: tuple[str, ...] = Field(min_length=1)
    tickets: tuple[str, ...] = ()
    # How recent a claim's newest row must be for the claim to count as live.
    live_window_minutes: int = Field(ge=1)
    why: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    # What to do when the read failed or its answer cannot be read (the verdict is UNKNOWN).
    unknown_fix: str = Field(min_length=1)


class ModelPlanOwnerOverlay(BaseModel):
    """Every deployment fact of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The reader name this node reads the open-claims answer under.
    reader: str = Field(pattern=_FINDING)
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
