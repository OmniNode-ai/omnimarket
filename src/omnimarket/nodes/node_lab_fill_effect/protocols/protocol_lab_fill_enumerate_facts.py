# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the approved-work and sprint-ticket enumerate operations (OMN-23250).

Every read the old enumerate made inline (the declared approved-work file, Linear, the PR
watcher's state, the clones' merged history, the triage fences) is one port here, so a stage
that is slow or down is named and the others still read. A port that runs out of the seconds
it was given raises ``LabFillSourceCutError``; one that cannot read raises ``LabFillPortError``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LabFillApprovedTicket:
    """One approved row's ticket as Linear holds it."""

    ticket: str
    state_type: str
    assignee_id: str
    updated_at: str
    description: str
    blockers: tuple[tuple[str, str], ...]
    prs: tuple[str, ...]
    merged_implementations: tuple[str, ...]


@dataclass(frozen=True)
class LabFillPrFacts:
    """What the PR watcher holds: tickets named by a merged PR, and the open PR keys by ticket."""

    merged_tickets: frozenset[str]
    open_tickets: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class LabFillMergedHistory:
    """The newest implementation merge naming each ticket, as (repo, merged_at_utc, sha), and the clones that could not be read."""

    newest: Mapping[str, tuple[str, str, str]]
    unread_repos: frozenset[str]


@dataclass(frozen=True)
class LabFillIssueNode:
    """One Linear issue of the sprint project, a defect or an assignment read."""

    ticket: str
    title: str
    state_type: str
    updated_at: str
    labels: tuple[str, ...]
    milestone: str
    project_id: str
    assignee_id: str
    has_children: bool
    readable: bool
    description_repo: str
    prs: tuple[str, ...]
    merged_implementations: tuple[str, ...]


@dataclass(frozen=True)
class LabFillWatcherPr:
    """One open, undrafted, unqueued PR of the watcher with the facts the red and stalled verdicts need."""

    repo: str
    number: int
    title: str
    head_ref: str
    labels: tuple[str, ...]
    updated_at: str
    idle_hours: float
    head_read: bool
    ci_head_matches: bool
    ci_verdict: str


class ProtocolLabFillApprovedRows(Protocol):
    """The declared approved-work file's rows exactly as declared (not yet validated)."""

    def __call__(self, path: str) -> Sequence[object]: ...


class ProtocolLabFillApprovedPrFacts(Protocol):
    """The PR watcher's merged tickets and open PRs; a stale watcher refuses."""

    def __call__(self) -> LabFillPrFacts: ...


class ProtocolLabFillMergedHistory(Protocol):
    """The merged history of the given tickets on the registry clones' default branches."""

    def __call__(
        self, tickets: Sequence[str], budget_s: float
    ) -> LabFillMergedHistory: ...


class ProtocolLabFillApprovedTickets(Protocol):
    """Linear's read of the given tickets (rows' tickets and their prerequisites)."""

    def __call__(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillApprovedTicket]: ...


class ProtocolLabFillAcceptanceProbe(Protocol):
    """Whether a command or path an acceptance check names exists for the row's repository."""

    def __call__(self, ref: str, repo: str) -> bool: ...


class ProtocolLabFillSprintIssues(Protocol):
    """The sprint project's issues, the defect issues, and the assignment read of PR tickets outside the project."""

    def project(
        self, project_id: str, state_types: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]: ...

    def defects(
        self, labels: Sequence[str], terms: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]: ...

    def tickets(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillIssueNode]: ...


class ProtocolLabFillWatcherPrs(Protocol):
    """The watcher's open PRs of the named repositories; a stale watcher refuses by naming unreadable-from-snapshot."""

    def __call__(self, repos: Sequence[str]) -> Sequence[LabFillWatcherPr]: ...


class ProtocolLabFillFences(Protocol):
    """The tickets the triage fences name."""

    def __call__(self, budget_s: float) -> tuple[str, ...]: ...
