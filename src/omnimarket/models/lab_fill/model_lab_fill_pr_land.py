# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and plan of the idle-slot pr-land fallback (OMN-20864).

Operator ruling 2026-10-10T04:08:23Z: when lab slots sit idle, the default work is a per-PR landing
lane (pr-land) on an open PR the landing controller parked, escalated or left red with no owner. Lab-fill
selection and the merge-throughput tick's lab-headroom finding both decide it with one rule
(``omnimarket.handlers.rules_lab_fill_pr_land``), so its facts and plan live here.

Every fact arrives typed in the request, read by the caller from the bus: PR state from
``pr-state-observed``, the red class from ``ci-red-triage-decided``, the landing state from
``pr-landing-transitioned``, the controller's park and escalation from its facts, and the hold rows from
the hold source. The rule reads no file.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing.enum_pr_landing_state import EnumPrLandingState
from omnimarket.models.ci_red_triage import EnumCiRedClass
from omnimarket.models.lab_fill.enum_lab_fill_skip_reason import EnumLabFillSkipReason

_FROZEN = ConfigDict(frozen=True, extra="forbid")
PR_KEY_PATTERN = r"^[A-Za-z0-9_.-]+#[0-9]+$"


class EnumLabFillPrLandCause(StrEnum):
    """A cause a per-PR landing lane cannot fix on the PR (AC-M8)."""

    REQUIRED_APPROVAL = "required-approval"
    BASE_RED = "base-red"
    HELD = "held"
    EXTERNAL_OWNER = "external-owner"


class EnumLabFillPrLandClass(StrEnum):
    """Why an open PR is the fallback's work, in dispatch order."""

    ESCALATED = "escalated"
    PARKED = "parked"
    UNOWNED_RED = "unowned-red"


class EnumLabFillPrLandFailure(StrEnum):
    """A fact the rule cannot decide without; the plan then dispatches nothing."""

    HOLD_SOURCE_UNREADABLE = "hold-source-unreadable"


class ModelLabFillPrLandOutcome(BaseModel):
    """The last landing lane's TERMINAL on the PR, with the cause it was blocked on, if typed."""

    model_config = _FROZEN

    outcome: str = Field(min_length=1)
    cause: EnumLabFillPrLandCause | None = None
    head_sha: str = ""
    at: str = ""
    lane: str = ""


class ModelLabFillOpenPr(BaseModel):
    """One open PR as the bus reports it."""

    model_config = _FROZEN

    pr: str = Field(pattern=PR_KEY_PATTERN, description="repo#number, bare repository")
    head_sha: str = Field(min_length=1)
    head_ref: str = ""
    title: str = ""
    ticket: str = Field(default="", description="The OMN id the PR names, if any.")
    draft: bool = False
    ci_verdict: Literal["GREEN", "RED", "PENDING", "NONE"] = "NONE"
    red_contexts: tuple[str, ...] = ()
    red_class: EnumCiRedClass | None = Field(
        default=None,
        description="ci-red-triage-decided's class for this head; None when unread.",
    )
    landing_state: EnumPrLandingState | None = Field(
        default=None,
        description="The newest pr-landing-transitioned to_state for this PR.",
    )
    controller_parked: bool = False
    controller_escalated: bool = False
    owner: str = Field(
        default="", description="A live lane or cause owner working this PR."
    )
    causes: tuple[EnumLabFillPrLandCause, ...] = Field(
        default=(), description="Unfixable causes the caller observed on the PR now."
    )
    cleared_causes: tuple[EnumLabFillPrLandCause, ...] = Field(
        default=(),
        description="Causes the caller observed cleared since the last landing outcome.",
    )
    last_outcome: ModelLabFillPrLandOutcome | None = None


class ModelLabFillHoldSource(BaseModel):
    """The hold rows (HOLD and RELEASE) as read from the source the deployment names."""

    model_config = _FROZEN

    source: str = Field(min_length=1)
    read: bool
    error: str = ""
    lines: tuple[str, ...] = ()


class ModelLabFillPrLandFacts(BaseModel):
    """Every open PR the fallback may land, and the holds over them."""

    model_config = _FROZEN

    prs: tuple[ModelLabFillOpenPr, ...] = ()
    holds: ModelLabFillHoldSource
    cooldown_hours: int = Field(default=6, ge=0)
    shared_red_min_prs: int = Field(
        default=3,
        ge=2,
        description=(
            "A red check on at least this many open PRs of one repository is a shared cause (the "
            "landing controller's cluster floor), which no per-PR lane fixes."
        ),
    )


class ModelLabFillPrLandDecision(BaseModel):
    """The first matching gate for one open PR, or eligibility (reason None)."""

    model_config = _FROZEN

    pr: str
    head_sha: str
    reason: EnumLabFillSkipReason | None
    pr_class: EnumLabFillPrLandClass | None = None
    detail: str = ""
    hold_id: str = ""
    cause: EnumLabFillPrLandCause | None = None


class ModelLabFillPrLandDispatch(BaseModel):
    """One pr-land lane to plan."""

    model_config = _FROZEN

    kind: Literal["pr-land"] = "pr-land"
    pr: str = Field(pattern=PR_KEY_PATTERN)
    repo: str
    head_sha: str
    head_ref: str = ""
    ticket: str = ""
    title: str = ""
    pr_class: EnumLabFillPrLandClass

    def as_candidate(self) -> dict[str, object]:
        """The lab-fill dispatch plan's candidate shape; a blank ticket takes the run's parent."""
        return {
            "kind": self.kind,
            "ticket": self.ticket,
            "pr": self.pr,
            "repo": self.repo,
            "head_ref": self.head_ref,
            "title": f"{self.pr_class.value}: {self.title}"[:160],
        }


class ModelLabFillPrLandPlan(BaseModel):
    """The fallback's decisions, its dispatches and why it planned fewer lanes than slots."""

    model_config = _FROZEN

    idle_slots: int = Field(ge=0)
    decisions: tuple[ModelLabFillPrLandDecision, ...] = ()
    dispatch: tuple[ModelLabFillPrLandDispatch, ...] = ()
    reason: str = ""
    failure: EnumLabFillPrLandFailure | None = None
