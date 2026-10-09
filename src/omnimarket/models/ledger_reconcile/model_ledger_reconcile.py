# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts and decisions that pass between the ledger-reconcile nodes (OMN-20677).

The orchestrator takes a ``ModelReconcileRequest`` and answers a
``ModelReconcileResult``. In between, the effect node reads the host's ledger and
verifies cited work (``ModelReconcileSources``, ``ModelReconcileFacts``); the
compute node decides from those alone (``ModelReconcileDecision``); the effect
node appends what was decided. Nothing here names a deployment: repository
aliases, branch prefixes and the GitHub organisation arrive in the overlay.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelReconcileRequest(BaseModel):
    """One reconciliation command."""

    model_config = _FROZEN

    correlation_id: UUID = Field(default_factory=uuid4)
    stale_hours: float = 6.0
    since_days: float = 7.0
    apply: bool = False
    max_appends: int | None = None
    live_lanes: frozenset[str] = frozenset()
    live_lanes_file: Path | None = None
    # True when live_lanes is the caller's complete current roster, even if empty.
    live_roster_known: bool = False
    silent_hours: float | None = None
    now: datetime | None = None


class ModelReconcileResult(BaseModel):
    """The terminal event, including the historical report text."""

    model_config = _FROZEN

    correlation_id: UUID
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    status: Literal["clean", "reconciled", "report-only", "blocked"]
    dangling: int = 0
    auto_closed: int = 0  # landed claims only
    abandoned: int = 0  # silent claims, completion unverified
    released: int = 0  # exact-id stale PR HOLD releases
    needs_attention: int = 0
    held: int = 0
    unknown: int = 0
    unparseable: int = 0
    commit_sha: str = ""
    notes: str = ""


class ModelReconcileOverlay(BaseModel):
    """The deployment facts the decisions read: never held by the node."""

    model_config = _FROZEN

    github_org: str
    repo_aliases: dict[str, str] = Field(default_factory=dict)
    branch_prefixes: tuple[str, ...] = ()


class ModelReconcileSource(BaseModel):
    """One ledger file as read: its name and its whole text."""

    model_config = _FROZEN

    name: str
    text: str


class ModelReconcileSources(BaseModel):
    """What the host holds: the ledger, its archives, the clones and the roster."""

    model_config = _FROZEN

    live: ModelReconcileSource
    archives: tuple[ModelReconcileSource, ...] = ()
    clones: tuple[str, ...] = ()
    registry_name: str
    live_lanes: tuple[str, ...] = ()
    overlay: ModelReconcileOverlay
    read_at: datetime


class ModelReconcileParams(BaseModel):
    """The command's bounds, validated by the compute node."""

    model_config = _FROZEN

    stale_hours: float = 6.0
    since_days: float = 7.0
    apply: bool = False
    max_appends: int | None = None
    silent_hours: float | None = None
    live_roster_known: bool = False
    now: datetime | None = None


class ModelPrRef(BaseModel):
    model_config = _FROZEN

    repo: str
    number: int


class ModelPrFact(BaseModel):
    """A PR as GitHub reports it; ``state`` is LOOKUP_FAILED when it could not be read."""

    model_config = _FROZEN

    repo: str
    number: int
    state: str
    merged_at: str = ""
    merge_sha: str = ""
    title: str = ""


class ModelPushFact(BaseModel):
    """When the PR's head commit was last committed, empty when unreadable."""

    model_config = _FROZEN

    repo: str
    number: int
    committed_at: str = ""


class ModelShaRef(BaseModel):
    """A commit and the clones it is looked for in, in order."""

    model_config = _FROZEN

    sha: str
    candidates: tuple[str, ...]


class ModelShaFact(BaseModel):
    model_config = _FROZEN

    sha: str
    candidates: tuple[str, ...]
    found_in: str = ""
    landed: bool = False
    committer_at: str = ""
    msg_tickets: tuple[str, ...] = ()


class ModelReconcileWanted(BaseModel):
    """Facts the decision needs and was not given."""

    model_config = _FROZEN

    prs: tuple[ModelPrRef, ...] = ()
    shas: tuple[ModelShaRef, ...] = ()
    pushes: tuple[ModelPrRef, ...] = ()

    def is_empty(self) -> bool:
        return not (self.prs or self.shas or self.pushes)


class ModelReconcileFacts(BaseModel):
    model_config = _FROZEN

    prs: tuple[ModelPrFact, ...] = ()
    shas: tuple[ModelShaFact, ...] = ()
    pushes: tuple[ModelPushFact, ...] = ()


class ModelPlannedAppend(BaseModel):
    """One row the decision wants appended; ``index`` names it in the outcomes."""

    model_config = _FROZEN

    index: int
    kind: Literal["terminal", "attention", "release"]
    row: str


class ModelAppendOutcome(BaseModel):
    model_config = _FROZEN

    index: int
    error: str = ""


class ModelReconcileDecision(BaseModel):
    """Decided, blocked, or waiting on facts the effect node must gather."""

    model_config = _FROZEN

    correlation_id: UUID
    status: Literal["decided", "needs-evidence", "blocked"]
    wanted: ModelReconcileWanted = ModelReconcileWanted()
    planned: tuple[ModelPlannedAppend, ...] = ()
    refused_by_cap: bool = False
    blocked_reason: str = ""
