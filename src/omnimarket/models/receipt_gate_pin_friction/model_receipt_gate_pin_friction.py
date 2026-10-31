# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed events and rows of the receipt-gate pin friction measurement (OMN-20134).

The pin updater (node_receipt_gate_pin_advance_effect) opens one pull request per
stale caller. The ruling of 2026-10-31T00:13:02Z keeps a full sha at one caller
(its pull_request_target audit requires one) and lets four callers hold a
dev-merge tag or a sha, "but we should measure friction overtime". These models
are what that measurement is made of:

* :class:`ModelPinOutcomeEvent` - one per caller outcome, published by the updater;
* the ``Model*Observation`` views - the few fields of the GitHub webhook
  observations the fold reads once a pin pull request exists;
* :class:`ModelPinFrictionRow` - what the projection keeps, one row per pin
  pull request (repository and branch);
* :class:`ModelPinFrictionEvent` - what a failed updater run or a touched pin pull
  request leaves, which the ledger effect turns into a FRICTION row;
* :class:`ModelPinFrictionFoldRequest` and :class:`ModelPinFrictionFoldResult` - one
  fold of an observation into a row, and the friction it newly caused.

Shared by the updater, the projection, the report and the ledger effect, so none of
them imports another node's private package.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_DEV_TAG = r"^dev-\d{14}-[0-9a-f]{12}$"
_SHA40 = r"^[0-9a-f]{40}$"

AwareTime = Annotated[datetime, AwareDatetime]


class EnumPinKind(StrEnum):
    """What the caller's pin site holds: the dev-merge tag itself, or the tag's commit sha."""

    SHA = "sha"
    TAG = "tag"


class EnumPinOutcomeKind(StrEnum):
    """What the updater did at one caller that needed a change."""

    OPENED = "opened"
    REPLAYED = "replayed"
    REFUSED = "refused"
    FAILED = "failed"


class EnumPinRefusalClass(StrEnum):
    """Why a pin change was refused or held up."""

    AUDIT = "audit"
    GUARD = "guard"
    CONFLICT = "conflict"
    UNMATCHED = "unmatched"
    OTHER = "other"


class EnumPinPrState(StrEnum):
    """Where one pin pull request stands."""

    OPENED = "opened"
    MERGED = "merged"
    CLOSED = "closed"
    FAILED = "failed"
    REFUSED = "refused"


class EnumPinFrictionReason(StrEnum):
    """Why a pin pull request is friction."""

    UPDATER_FAILED = "updater-failed"
    HUMAN_TOUCHED = "human-touched"


class ModelPinOutcomeEvent(BaseModel):
    """One pin-advance outcome at one caller, published by the updater."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["outcome"] = "outcome"
    outcome: EnumPinOutcomeKind
    pin_kind: EnumPinKind
    repository: str = Field(pattern=_REPO)
    core_tag: str = Field(pattern=_DEV_TAG)
    branch: str = Field(min_length=1)
    branch_head_sha: str | None = Field(default=None, pattern=_SHA40)
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: str | None = None
    tag_pushed_at: AwareTime | None = Field(
        default=None, description="When the source repository's tag was pushed."
    )
    outcome_at: AwareTime = Field(
        description="When the updater reached this outcome (the pull request's opening time)."
    )
    seconds_tag_to_pr_opened: float | None = Field(default=None, ge=0)
    refusal_class: EnumPinRefusalClass | None = None
    detail: str = ""
    delivery_id: UUID | None = None

    @model_validator(mode="after")
    def _shape_follows_outcome(self) -> ModelPinOutcomeEvent:
        opened = self.outcome in {
            EnumPinOutcomeKind.OPENED,
            EnumPinOutcomeKind.REPLAYED,
        }
        if opened and self.branch_head_sha is None:
            raise ValueError("an opened outcome names the head sha it published")
        if not opened and self.refusal_class is None:
            raise ValueError("a refused or failed outcome names its refusal class")
        return self


class ModelPinMergedObservation(BaseModel):
    """A merged pull request, as onex.evt.github.pr-merged.v1 reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["merged"] = "merged"
    repo: str = Field(pattern=_REPO)
    branch: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    merged_at: AwareTime


class ModelPinHeadObservation(BaseModel):
    """A pull request's head, state and mergeability, as onex.evt.github.pr-status.v1 reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["head"] = "head"
    repo: str = Field(pattern=_REPO)
    head_ref: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    as_of: AwareTime
    head_sha: str | None = None
    triage_state: str | None = None
    mergeable: str | None = None


class ModelPinRunObservation(BaseModel):
    """A workflow run state change, as onex.evt.github.workflow-run.v1 reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["run"] = "run"
    repo: str = Field(pattern=_REPO)
    run_id: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    status: str
    as_of: AwareTime
    run_attempt: int | None = Field(default=None, ge=1)


class ModelPinCheckObservation(BaseModel):
    """A red check run on a pull request, as onex.evt.github.check-run.v1 reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["check"] = "check"
    repo: str = Field(pattern=_REPO)
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    check: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    completed_at: AwareTime


class ModelPinRefusal(BaseModel):
    """One refusal or hold-up a pin pull request met."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_class: EnumPinRefusalClass
    detail: str = ""
    at: AwareTime


class ModelPinFrictionRow(BaseModel):
    """What the projection keeps for one pin pull request (keyed by repository and branch).

    ``extra="ignore"``: a row read back from the projection table also carries
    the table's bookkeeping columns.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    repository: str = Field(pattern=_REPO)
    branch: str = Field(min_length=1)
    pin_kind: EnumPinKind
    core_tag: str = Field(pattern=_DEV_TAG)
    state: EnumPinPrState
    first_seen_at: AwareTime
    pr_number: int | None = Field(default=None, ge=1)
    tag_pushed_at: AwareTime | None = None
    pr_opened_at: AwareTime | None = None
    merged_at: AwareTime | None = None
    closed_at: AwareTime | None = None
    seconds_tag_to_pr_opened: float | None = Field(default=None, ge=0)
    seconds_pr_open_to_merge: float | None = Field(default=None, ge=0)
    seconds_tag_to_merge: float | None = Field(default=None, ge=0)
    published_head_sha: str | None = None
    head_shas: tuple[str, ...] = ()
    rerun_keys: tuple[str, ...] = Field(
        default=(),
        description="'<run id>:<attempt>' of each CI re-run (attempt above 1).",
    )
    refusals: tuple[ModelPinRefusal, ...] = ()
    friction_reasons: tuple[EnumPinFrictionReason, ...] = ()
    last_event_at: AwareTime

    @property
    def fix_pushes(self) -> int:
        """Heads beyond the one the updater published."""
        return max(len(self.head_shas) - 1, 0)

    @property
    def ci_reruns(self) -> int:
        return len(self.rerun_keys)

    @property
    def touches(self) -> int:
        """Fix pushes plus CI re-runs: what a lane or a person did after the updater."""
        return self.fix_pushes + self.ci_reruns

    @property
    def merged_untouched(self) -> bool:
        return self.state is EnumPinPrState.MERGED and self.touches == 0


class ModelPinFrictionEvent(BaseModel):
    """A pin pull request that is friction: the updater failed, or someone touched it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumPinFrictionReason
    site: str = Field(pattern=_REPO, description="The caller repository.")
    pin_kind: EnumPinKind
    core_tag: str = Field(pattern=_DEV_TAG)
    branch: str = Field(min_length=1)
    pr_number: int | None = Field(default=None, ge=1)
    fix_pushes: int = Field(default=0, ge=0)
    ci_reruns: int = Field(default=0, ge=0)
    refusal_classes: tuple[EnumPinRefusalClass, ...] = ()
    detail: str = ""
    flagged_at: AwareTime


PinObservationUnion = Annotated[
    ModelPinOutcomeEvent
    | ModelPinMergedObservation
    | ModelPinHeadObservation
    | ModelPinRunObservation
    | ModelPinCheckObservation,
    Field(discriminator="kind"),
]


class ModelPinFrictionFoldRequest(BaseModel):
    """One observation of a pin pull request and the row it is folded into.

    ``prior_row`` is the pin pull request's row as the caller holds it (None when it
    holds none): the reducer keeps no state, the caller persists what comes back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_row: ModelPinFrictionRow | None = None
    observation: PinObservationUnion


class ModelPinFrictionFoldResult(BaseModel):
    """The pin pull request's next row, and the friction the observation newly caused.

    ``row`` is None when the observation names no pin pull request (a merge of a
    branch the updater never reported). One observation causes at most one friction
    event: a failed updater run needs no pull request and a touch needs one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelPinFrictionRow | None = None
    changed: bool = False
    friction_event: ModelPinFrictionEvent | None = None


__all__ = [
    "AwareTime",
    "EnumPinFrictionReason",
    "EnumPinKind",
    "EnumPinOutcomeKind",
    "EnumPinPrState",
    "EnumPinRefusalClass",
    "ModelPinCheckObservation",
    "ModelPinFrictionEvent",
    "ModelPinFrictionFoldRequest",
    "ModelPinFrictionFoldResult",
    "ModelPinFrictionRow",
    "ModelPinHeadObservation",
    "ModelPinMergedObservation",
    "ModelPinOutcomeEvent",
    "ModelPinRefusal",
    "ModelPinRunObservation",
    "PinObservationUnion",
]
