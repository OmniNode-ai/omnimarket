# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the Linear git-automation drift guard (OMN-15373).

Linear teams carry a ``GitAutomationState`` per git event (``draft`` / ``start``
/ ``review`` / ``merge``). Each one names the workflow state a linked issue is
moved to when that event fires. When the ``merge`` automation names a state
whose ``type`` is ``completed``, **every** PR merge flips **every** linked issue
straight to Done in ~2-3 seconds — with no closing keyword required (plain
branch-name linkage or a bare body mention is sufficient) and no evidence gate
of any kind on the path.

That is exactly the OMN-15373 incident: 16 confirmed merge-time auto-flips
between 2026-07-28T22:00Z and 2026-07-29T02:30Z, every one 2.0-3.1s after its
driving PR merged. It violates the standing rule that ``Done`` is reachable only
via ``dod_verify`` with durable evidence — a merge is ``code-only`` /
``receipt-bound`` at best.

The team setting was corrected by hand on 2026-07-29T02:29:19Z, but a workspace
setting one API call (or one UI click) can silently revert is **not a
mechanism** (``feedback_a_rule_is_not_a_mechanism``). These models describe the
observed automation configuration and the guard's fail-closed verdict over it,
so the assertion can be run on a schedule and fail loudly on drift.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Linear's WorkflowState.type value that means "this issue is finished". Any git
# automation resolving to a state of this type mints Done with zero proof.
COMPLETED_STATE_TYPE = "completed"


class EnumGitAutomationVerdict(StrEnum):
    """Per-automation verdict.

    * ``CLEAN`` — the automation resolves to a non-``completed`` state (or to no
      state at all), so firing it cannot mint a Done.
    * ``DRIFT`` — the automation resolves to a ``completed``-type state. Firing
      it mints an evidence-less Done. This is the OMN-15373 defect.
    * ``ACCEPTED_EXCEPTION`` — a ``completed``-type target explicitly registered
      as accepted, by an owner, with an absolute expiry that has not passed.
      Still reported; does not fail the guard until the expiry lapses.
    * ``UNREADABLE`` — the probe could not resolve this automation's target
      state. Treated as failing (never as CLEAN): an unreadable check is not a
      passing check.
    """

    CLEAN = "clean"
    DRIFT = "drift"
    ACCEPTED_EXCEPTION = "accepted_exception"
    UNREADABLE = "unreadable"


class ModelGitAutomationState(BaseModel):
    """One team's git automation for one git event, as read back from Linear.

    Mirrors the Linear ``GitAutomationState`` shape:
    ``{ id, event, state { id name type }, targetBranch }``. ``state_type`` is
    empty when the automation names no state OR when the probe could not read
    it — the two are distinguished by ``state_readable``, because "no state
    configured" is genuinely CLEAN while "could not read" must fail closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_key: str
    team_id: str = ""
    automation_id: str
    # "draft" | "start" | "review" | "merge"
    event: str
    state_id: str = ""
    state_name: str = ""
    # Linear WorkflowState.type: "backlog" | "unstarted" | "started" |
    # "completed" | "canceled" | "triage". Empty means no state configured.
    state_type: str = ""
    # False => the probe could not resolve the target state; fail closed.
    state_readable: bool = True
    # None/empty => the automation applies to EVERY branch in EVERY repo.
    target_branch: str | None = None


class ModelGitAutomationException(BaseModel):
    """An explicitly-accepted ``completed``-type automation target.

    An exception is a dated decision, never a permanent silencer: ``expires_at``
    is REQUIRED and absolute, so an accepted exception re-fails the guard the
    moment it lapses. ``owner`` and ``reason`` are required and non-blank so the
    registry can never accumulate anonymous suppressions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    automation_id: str
    team_key: str
    owner: str
    reason: str
    expires_at: datetime


class ModelGitAutomationFinding(BaseModel):
    """The guard's verdict for one automation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_key: str
    automation_id: str
    event: str
    state_name: str = ""
    state_type: str = ""
    verdict: EnumGitAutomationVerdict
    reason: str
    # True when the automation applies to every branch of every repo.
    all_branches: bool = False


class ModelGitAutomationAuditReport(BaseModel):
    """Aggregate guard report over every automation on every team.

    ``passed`` is the single fail-closed signal the scheduled runner exits on.
    It is True ONLY when the probe succeeded, returned a non-empty automation
    set, and every finding is CLEAN or a live ACCEPTED_EXCEPTION.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    findings: list[ModelGitAutomationFinding] = Field(default_factory=list)
    drift_count: int = 0
    accepted_count: int = 0
    unreadable_count: int = 0
    clean_count: int = 0
    # Populated when the guard fails for a reason other than a per-automation
    # finding (probe error, empty result set).
    failure_reason: str = ""


__all__ = [
    "COMPLETED_STATE_TYPE",
    "EnumGitAutomationVerdict",
    "ModelGitAutomationAuditReport",
    "ModelGitAutomationException",
    "ModelGitAutomationFinding",
    "ModelGitAutomationState",
]
