# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the code graph (rebuild piece 1, OMN-22651).

The graph is the Python import graph of the declared repositories at their default-branch heads:
one entity per module file, an edge per import, and reachability from the declared roots. A run
judges itself against a frozen sample with known-used controls and appends one reading row.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumGraphFault(StrEnum):
    """A fault injected on purpose. A run with one is a drill and its row says so."""

    NONE = "none"
    #: The first declared repository cannot be read.
    UNREADABLE_REPO = "unreadable-repo"
    #: Contracts stop linking their node packages to the modules they name.
    DROP_CONTRACT_LINKS = "drop-contract-links"


class ModelRepoSnapshot(BaseModel):
    """The files of one repository at one commit, as the graph reads them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    head: str = Field(min_length=1)
    #: Repository-relative path to text, only the files the graph reads.
    files: dict[str, str]


class ModelRepoGraph(BaseModel):
    """What the graph found in one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    head: str
    modules: int = Field(ge=0)
    test_modules: int = Field(ge=0)
    roots: int = Field(ge=0)
    reachable: int = Field(ge=0)
    #: Non-test module paths no root reaches, sorted.
    unreachable: tuple[str, ...] = ()
    #: Of those, the ones a test imports (referenced only by tests), sorted.
    test_only: tuple[str, ...] = ()


class ModelSampleOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agree: int = Field(ge=0)
    total: int = Field(ge=0)
    controls_reached: int = Field(ge=0)
    controls_total: int = Field(ge=0)
    #: Token-safe reasons for each disagreement and each control not reached.
    misses: tuple[str, ...] = ()


class ModelCodeGraphCommand(BaseModel):
    """Build the graph of every declared repository now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: A probe run builds and judges and appends nothing.
    write: bool = True
    #: A run with a fault is a drill: its row carries drill=yes and no operational reading reads it.
    fault: EnumGraphFault = EnumGraphFault.NONE


class ModelCodeGraphResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...] = Field(min_length=1)
    fault: EnumGraphFault = EnumGraphFault.NONE
    repos: tuple[ModelRepoGraph, ...] = ()
    sample: ModelSampleOutcome | None = None
    row_key: str | None = None
    #: ``written``, ``already-written``, ``not-written`` (a probe) or ``append-failed:<why>``.
    ledger: str = "not-written"
    row: str | None = None
    #: Why the run failed: a repository that could not be read, or an append that did not land.
    failure: str | None = None


class ModelGraphCheckResult(BaseModel):
    """A pull request's head against its base: modules it adds that no root reaches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    base: str
    head: str
    added: int = Field(ge=0)
    #: Added non-test module paths unreachable at the head and not allowed, sorted.
    flagged: tuple[str, ...] = ()
    #: Added unreachable paths a live allowlist entry excuses, sorted.
    allowed: tuple[str, ...] = ()
    #: Allowlist entries of this repository past their date, each ``<path> expired <date>``.
    expired: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.flagged and not self.expired


class EnumShadowOutcome(StrEnum):
    """What the check would have decided for one pull request."""

    WOULD_PASS = "would-pass"
    WOULD_REFUSE = "would-refuse"
    #: A ref could not be read: no verdict, and the row says so.
    INDETERMINATE = "indeterminate"


class ModelShadowPullRequest(BaseModel):
    """An open pull request the shadow checks: its base branch and the exact head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(ge=1)
    base_ref: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)


class ModelShadowVerdict(BaseModel):
    """The check's verdict for one pull request, logged and never enforced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    head: str
    outcome: EnumShadowOutcome
    added: int = Field(ge=0, default=0)
    flagged: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()
    #: Why the verdict is indeterminate; empty otherwise.
    reason: str = ""


class ModelShadowCommand(BaseModel):
    """Check every open pull request of the declared repositories now, in shadow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: A probe run checks and appends nothing.
    write: bool = True
    #: A run with a fault is a drill: its row carries drill=yes.
    fault: EnumGraphFault = EnumGraphFault.NONE


class ModelShadowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    fault: EnumGraphFault = EnumGraphFault.NONE
    repos: tuple[str, ...] = ()
    verdicts: tuple[ModelShadowVerdict, ...] = ()
    #: The shadow decides nothing: it never holds a merge, whatever it would have refused.
    holds_merge: Literal[False] = False
    row_key: str | None = None
    ledger: str = "not-written"
    row: str | None = None
    failure: str | None = None

    def count(self, outcome: EnumShadowOutcome) -> int:
        return sum(1 for verdict in self.verdicts if verdict.outcome is outcome)
