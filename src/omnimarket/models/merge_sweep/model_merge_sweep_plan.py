# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep lane plan (OMN-20676).

The reading is what the sweep reader gathered and evaluated; the reader emits more fields than the
plan uses (host, components, unread, ...), so the reading models ignore extra keys. An empty
``controller``, ``product`` or ``owner`` object reads as absent, as the rules module read it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_READING = ConfigDict(frozen=True, extra="ignore")

DEFAULT_MAX_LANES = 8
MAX_LANES_CEILING = 12
# The record a real (code) failure is named under: the plan issues no command for it.
DEFAULT_REAL_FAILURE_RECORD = "OMN-23832"


def _empty_as_absent(value: Any) -> Any:
    """An empty mapping is absent: the rules module tested these with ``or``."""
    return None if isinstance(value, dict) and not value else value


class ModelMergeSweepOwner(BaseModel):
    """Who owns a PR now, as the claim-owner reading evaluated it."""

    model_config = _READING

    state: str | None = None
    lane: str | None = None
    claim_ts: str | None = None


class ModelMergeSweepController(BaseModel):
    """The landing controller's stall reading."""

    model_config = _READING

    stalled: bool = False
    reasons: list[str] = Field(default_factory=list)


class ModelMergeSweepProduct(BaseModel):
    """The product-merge reading; only the repositories under their floor decide a lane."""

    model_config = _READING

    under_floor: list[str] = Field(default_factory=list)


class _ModelOwned(BaseModel):
    """A reading row with an owner; an empty owner object reads as absent."""

    model_config = _READING

    @model_validator(mode="before")
    @classmethod
    def _owner_absent_when_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {**data, "owner": _empty_as_absent(data.get("owner"))}
        return data


class ModelMergeSweepEscalation(_ModelOwned):
    """A PR the controller's newest degraded list names escalation_exhausted."""

    pr: str
    state: str = "UNKNOWN"
    owner: ModelMergeSweepOwner | None = None


class _ModelWriteFacts(_ModelOwned):
    """The facts a landing command is pinned to; None is unread, never a zero (OMN-21070 fills them).

    ``repository`` is the owner/name slug (it falls back to ``repo`` when that is already one),
    ``head_sha`` the head the sweep read, ``base_ref`` its base branch, ``pr_node_id`` the GraphQL
    id an arm or enqueue needs, and ``merge_queue`` whether the base lands through a merge queue.
    """

    repository: str | None = None
    head_sha: str | None = None
    base_ref: str | None = None
    pr_node_id: str | None = None
    merge_queue: bool | None = None


class ModelMergeSweepChainHead(_ModelWriteFacts):
    """An open PR on a default branch whose head branch is the base of other open PRs."""

    repo: str
    number: int
    cls: str | None = None
    state: str = "UNKNOWN"
    owner: ModelMergeSweepOwner | None = None
    children: list[int] = Field(default_factory=list)


class ModelMergeSweepRed(_ModelWriteFacts):
    """An open PR with a red check, classified on the newest run of each check name.

    Each class entry carries the newest run's ``run_id`` (``classify_checks``), which a rerun names.
    """

    repo: str
    number: int
    state: str = "UNKNOWN"
    ticket: str | None = None
    classes: list[dict[str, Any]] | None = None
    owner: ModelMergeSweepOwner | None = None


class ModelMergeSweepIssuedCommand(BaseModel):
    """A landing command already issued for one PR at one head; the plan never issues it twice."""

    model_config = _READING

    repo: str
    number: int
    head_sha: str
    operation: str


class ModelMergeSweepReading(BaseModel):
    """One reading of the fleet, as the sweep reader evaluated it."""

    model_config = _READING

    now: str | None = None
    load1: float | None = None
    cpus: float | None = None
    scope: list[str] | None = None
    controller: ModelMergeSweepController | None = None
    product: ModelMergeSweepProduct | None = None
    escalations: list[ModelMergeSweepEscalation] | None = None
    chain_heads: list[ModelMergeSweepChainHead] | None = None
    reds: list[ModelMergeSweepRed] | None = None
    issued_commands: list[ModelMergeSweepIssuedCommand] | None = None

    @model_validator(mode="before")
    @classmethod
    def _absent_when_empty(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            **data,
            "controller": _empty_as_absent(data.get("controller")),
            "product": _empty_as_absent(data.get("product")),
        }


class ModelMergeSweepPlanRequest(BaseModel):
    """The reading to plan from, and the dispatch cap (None or 0 reads as the default).

    ``landing_mode`` is the mode every landing command carries; the plan is watch-only, so a request
    asking for enforce is refused. ``correlation_id`` is the sweep run's own: the commands carry it
    so the landing effect's answers read back under it (absent, each command's own is derived from
    its idempotency key). ``real_failure_record`` names where a real failure is recorded.
    """

    model_config = _FROZEN

    reading: ModelMergeSweepReading
    max_lanes: int | None = None
    landing_mode: EnumPrLandingGithubMode = EnumPrLandingGithubMode.DRY_RUN
    correlation_id: UUID | None = None
    real_failure_record: str = DEFAULT_REAL_FAILURE_RECORD

    @field_validator("landing_mode")
    @classmethod
    def _watch_only(cls, value: EnumPrLandingGithubMode) -> EnumPrLandingGithubMode:
        if value is not EnumPrLandingGithubMode.DRY_RUN:
            raise ValueError(
                f"the merge sweep plan is watch-only: landing_mode must be dry_run, got {value.value}"
            )
        return value


class ModelMergeSweepLanePr(BaseModel):
    """One PR in a lane. Keys the rules did not set stay unset; dump with ``exclude_unset``."""

    model_config = _FROZEN

    pr: str
    supersedes_claim: str | None = None
    children: list[int] = Field(default_factory=list)
    ticket: str | None = None
    classes: list[dict[str, Any]] = Field(default_factory=list)


class ModelMergeSweepLane(BaseModel):
    """One lane a sweep dispatches: diagnose, escalation, land-chain-head or fix."""

    model_config = _FROZEN

    kind: str
    repo: str | None
    prs: list[ModelMergeSweepLanePr]
    reasons: list[str] = Field(default_factory=list)


class ModelMergeSweepSkipped(BaseModel):
    """A PR the plan left out, with the reason."""

    model_config = _FROZEN

    pr: str
    why: str


class ModelMergeSweepLandingCommand(BaseModel):
    """One command for the landing effect, with what task S2 adds to the effect's request.

    ``request`` is the landing effect's own request (``mode`` dry_run). ``idempotency_key`` is
    once per (repository, PR, head, operation), so a second sender never repeats it; ``reason``
    names the red class and remedy that planned it.
    """

    model_config = _FROZEN

    request: ModelPrLandingGithubRequest
    idempotency_key: str
    reason: str


class ModelMergeSweepPlanResult(BaseModel):
    """The plan: every lane in order, what was skipped, and the lanes this sweep dispatches.

    ``landing_commands`` are the watch-only commands for the reds whose remedy is a landing
    operation; ``landing_skipped`` is each remedy left without a command, and why. A change with
    commands gets no lane (unless a real failure remains on it).
    """

    model_config = _FROZEN

    now: str | None
    lab_only: bool
    load_per_core: float | None
    lanes: list[ModelMergeSweepLane]
    skipped: list[ModelMergeSweepSkipped]
    dispatch: list[ModelMergeSweepLane]
    deferred: int
    landing_commands: list[ModelMergeSweepLandingCommand] = Field(default_factory=list)
    landing_skipped: list[ModelMergeSweepSkipped] = Field(default_factory=list)


DEFAULT_CLAIM_CHECK_COMMAND = (
    'python3 "${CLAUDE_PLUGIN_ROOT}/skills/merge-sweep/scripts/sweep_read.py" '
    "--claim-check <repo>#<n>"
)


class ModelMergeSweepBriefRequest(BaseModel):
    """One lane to brief: its name, its kind, the PRs it covers and why the sweep planned it.

    ``sweep_lane`` is the sweep's own lane and ``orchestrator`` its parent. ``claim_check_command``
    is the recheck every lane runs before it touches a PR.
    """

    model_config = _FROZEN

    lane: str
    sweep_lane: str
    orchestrator: str
    ticket: str
    kind: str
    repo: str | None = None
    prs: list[ModelMergeSweepLanePr] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    claim_check_command: str = DEFAULT_CLAIM_CHECK_COMMAND


class ModelMergeSweepBrief(BaseModel):
    """The lane's brief, as the runner is given it."""

    model_config = _FROZEN

    text: str
