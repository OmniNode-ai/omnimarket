# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable facts for lab-fill selection (OMN-20662)."""

from __future__ import annotations

from dataclasses import dataclass

from .model_lab_fill_deployment import ModelLabFillDeployment


@dataclass(frozen=True, slots=True)
class ModelLabFillCandidateInput:
    """A candidate and the external facts read by Enumerate."""

    key: str
    kind: str
    ticket: str
    pr: str = ""
    repo: str = ""
    claim_holder: str = ""
    ticket_updated_at: str = ""
    pr_heads: tuple[str, ...] = ()
    main_sha: str = ""
    watcher_read: bool = False
    facts_read_at: str = ""
    # Repository identity of a defect (OMN-20662).
    title: str = ""
    labels: tuple[str, ...] = ()
    project_id: str = ""
    # Where the work lives (OMN-17427): the PRs that name the ticket, newest first (the watcher's
    # open and merged PRs and the ticket's GitHub attachments), the merged ones among them, and the
    # text (goal, acceptance check) read only for the repository paths it names.
    ticket_prs: tuple[str, ...] = ()
    merged_prs: tuple[str, ...] = ()
    body: str = ""


@dataclass(frozen=True, slots=True)
class ModelLandingControllerFacts:
    """Controller ownership and PRs escalated back for repair."""

    read: bool = False
    held_prs: tuple[str, ...] = ()
    escalated_prs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelLabFillInputBaseline:
    """Inputs observed after a lab-fill lane's terminal."""

    ticket: str
    lane: str
    outcome: str
    ticket_updated_at: str
    pr_heads: tuple[str, ...]
    main_sha: str
    watcher_read: bool
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ModelLabFillSelectionRequest:
    """All facts needed for deterministic selection and baseline retention."""

    now: str
    candidates: tuple[ModelLabFillCandidateInput, ...]
    ledger_lines: tuple[str, ...]
    deployment: ModelLabFillDeployment
    controller: ModelLandingControllerFacts = ModelLandingControllerFacts()
    baselines: tuple[ModelLabFillInputBaseline, ...] = ()
    closed_prs: tuple[str, ...] = ()
    landing_lane: str = "landing-controller"
    attempt_limit: int = 2
    attempt_window_hours: int = 24
    # A ticket held for unchanged input after a blocked, partial or no-op lane is tried again this
    # long after that lane's TERMINAL even when nothing changed (OMN-17427).
    cooldown_hours: int = 6
    project_id: str = ""  # the declared sprint project: its defects are in scope
    scope_repos: tuple[
        str, ...
    ] = ()  # repositories lab-fill works in; empty disables the scope rule
