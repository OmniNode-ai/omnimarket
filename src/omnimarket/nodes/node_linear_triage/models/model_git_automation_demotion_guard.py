# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the git-automation demotion ratchet (OMN-16536 AC#2).

The defect
----------
A verified-``Done`` ticket is silently reverted the moment any PR cites it —
including a PR for an unrelated ticket that merely names it in a non-closing
``Refs:`` line, and including a draft PR flipped to ``ready_for_review``. No
closing keyword is required. Measured signature: the Linear transition lands
2.3-3.2 seconds after the driving PR event, with no human actor.

Why the target state alone decides it
-------------------------------------
Linear's ``GitAutomationState`` has **no source-state predicate**. It names one
target workflow state per git event (``draft`` / ``start`` / ``review`` /
``merge``) and moves every linked issue there *unconditionally* — whatever the
issue's current state, whatever evidence does or does not exist.

So "resolves a ``completed`` ticket to a non-``completed`` state" is not a
property of some ticket/automation pair that has to be simulated. It is a
property of the automation's target alone: **any** automation whose target type
is not ``completed`` demotes a verified-Done ticket whenever it fires. That
collapses AC#2 into a cheap, total ratchet:

    per team:  len(gitAutomationStates) == 0
               OR every automation targets a completed-type state

The 2026-08-27 AC#1 readback confirmed the collapse empirically: all ten live
mappings across teams OMN, CON and JON targeted ``started``-type states, so all
ten satisfied the failure predicate — not just the ``merge`` one that OMN-15373
had retargeted. Deleting only ``merge`` would have left ``draft``, ``start`` and
``review`` still demoting.

Mirror of the OMN-15373 guard
-----------------------------
``model_git_automation_guard`` asserts the opposite polarity — no automation may
target a ``completed`` state, because that mints Done with no evidence. The
conjunction of both assertions is ``len(gitAutomationStates) == 0``, which is
exactly the state AC#1 established. The two are kept separate rather than fused
because they are owned by different tickets and carry different exception
registries; relaxing one must never silently relax the other.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    COMPLETED_STATE_TYPE,
    ModelGitAutomationState,
)

__all__ = [
    "COMPLETED_STATE_TYPE",
    "EnumDemotionVerdict",
    "ModelDemotionAuditReport",
    "ModelDemotionFinding",
    "ModelTeamAutomationProbe",
]


class EnumDemotionVerdict(StrEnum):
    """Per-automation verdict for the demotion ratchet.

    * ``CLEAN`` — the target is a ``completed``-type state, so firing this
      automation cannot demote a completed ticket. (Such a target is
      simultaneously ``DRIFT`` for the OMN-15373 guard; both must hold.)
    * ``DEMOTION_RISK`` — the target is a non-``completed`` state. Because the
      transition is unconditional, firing it knocks a verified-Done ticket back
      to a non-completed state. This is the OMN-16536 defect.
    * ``UNREADABLE`` — the probe could not resolve this automation's target
      state. Treated as failing, never as CLEAN: an unreadable check is not a
      passing check.
    """

    CLEAN = "clean"
    DEMOTION_RISK = "demotion_risk"
    UNREADABLE = "unreadable"


class ModelTeamAutomationProbe(BaseModel):
    """The result of reading one team's ``gitAutomationStates``.

    Per-team rather than org-wide by design. The nested
    ``teams { gitAutomationStates }`` query's complexity is the PRODUCT of both
    page sizes and breaches Linear's 10000 cap at ``50 x 50`` (measured 11565 on
    2026-08-27), which forces the nested form down to ``first: 10`` on
    automations — a page that silently truncates a team carrying 11. Reading one
    team at a time keeps each query's complexity constant as the workspace grows
    and allows the full ``first: 50`` automation page.

    ``probe_ok=False`` records a team that could not be read. It is never
    conflated with a team that was read and found empty — the whole point of the
    ratchet's positive control.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_key: str
    team_id: str = ""
    # False => this team could not be read; the audit fails closed.
    probe_ok: bool = True
    probe_error: str = ""
    automations: list[ModelGitAutomationState] = Field(default_factory=list)
    # The page size the automations were requested with. A returned count equal
    # to it cannot prove it was the last page, so it fails closed as possible
    # truncation.
    page_size: int = 50

    @property
    def possibly_truncated(self) -> bool:
        """True when the returned page was exactly full, so completeness is
        unproven."""
        return self.page_size > 0 and len(self.automations) >= self.page_size


class ModelDemotionFinding(BaseModel):
    """The ratchet's verdict for one automation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_key: str
    automation_id: str
    event: str
    state_name: str = ""
    state_type: str = ""
    verdict: EnumDemotionVerdict
    reason: str
    # True when the automation applies to every branch of every repo.
    all_branches: bool = False


class ModelDemotionAuditReport(BaseModel):
    """Aggregate ratchet report over every automation on every team.

    ``passed`` is the single fail-closed signal the scheduled runner exits on.
    It is True ONLY when the positive control held (team enumeration succeeded,
    returned at least one team, and every enumerated team was actually probed
    without error or possible truncation) AND no finding is ``DEMOTION_RISK`` or
    ``UNREADABLE``.

    Note the deliberate asymmetry with the OMN-15373 report: there, an EMPTY
    automation set fails, because empty was indistinguishable from a broken
    probe. Here empty is the *expected* green state after AC#1 deleted all ten
    mappings, so emptiness is made trustworthy by the positive control instead
    of by refusing to accept it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    # False => the run proved nothing, whatever the findings say.
    positive_control_ok: bool = False
    teams_enumerated: int = 0
    teams_probed: int = 0
    findings: list[ModelDemotionFinding] = Field(default_factory=list)
    demotion_risk_count: int = 0
    unreadable_count: int = 0
    clean_count: int = 0
    # Populated when the guard fails for a reason other than a per-automation
    # finding (enumeration failure, unprobed team, per-team error, truncation).
    failure_reason: str = ""
