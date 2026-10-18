# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep reading (OMN-20676).

The facts arrive already gathered from the PR watcher's state file, the rolling ledger and the
landing controller's tick log, by node_merge_sweep_effect. The watcher does not record a PR's
changed files, its last ``ready_for_review`` time or a merge's changed files: each such fact is
``None`` or listed in ``facts_unread``, and the reading names it under ``unread``. An unread fact
is never read as a zero.

A request the reading cannot evaluate as asked is refused by name: a ``now`` that is not UTC, an
empty fleet, an out-of-scope repository with no ruling, a ``checks_state`` that is not one of its
four forms.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CHECKS_READ = "read"
CHECKS_ROLLUP_ONLY = "rollup-only"
CHECKS_SKIPPED = "skipped:"
CHECKS_UNREAD = "unread:"
NO_CHECK_READ = "the state holds no check read at this head"


def utc_stamp(value: str) -> str:
    """``value`` when it is a UTC time; a naive or offset time is refused by name."""
    try:
        got = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"now {value!r} is not an ISO time") from exc
    if got.utcoffset() != timedelta(0):
        raise ValueError(f"now {value!r} is not UTC: write it with Z or +00:00")
    return value


UtcStamp = Annotated[str, AfterValidator(utc_stamp)]


class ModelSweepRun(BaseModel):
    """The newest copy of one check name at a PR's head."""

    model_config = _FROZEN

    name: str
    id: int
    started_at: str | None = None
    status: str
    conclusion: str | None = None


class ModelSweepOpenPr(BaseModel):
    """An open PR as the watcher state holds it.

    ``checks_state`` says what the state answered for the checks at this head, and decides the
    reading:

    - ``read``: ``runs`` holds the newest copy of every check name, joined to its run.
    - ``rollup-only``: the rollup at the head is red and the detail that names each run is unread
      (``runs_why`` says why); the red is kept with its classes unread.
    - ``skipped:<why>``: the watcher does not read this PR's CI by its own rule (a draft, a hold
      label, a production gate). That is its answer, not an unread fact; nothing is recorded.
    - ``unread:<why>``: the state holds no check read at this head.

    Absent, it follows ``runs``: ``read`` when they are present, else ``unread:`` with ``runs_why``.
    ``files`` and ``ready_at`` come from a source richer than the state when one exists;
    ``facts_unread`` names those the source could not supply.
    """

    model_config = _FROZEN

    repo: str
    number: int
    title: str = ""
    base: str
    head_ref: str
    draft: bool = False
    runs: list[ModelSweepRun] | None = None
    runs_why: str = ""
    checks_state: str = ""
    files: list[str] = Field(default_factory=list)
    ready_at: str | None = None
    facts_unread: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _checks_state_follows_runs(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "checks_state" in data:
            return data
        if data.get("runs") is not None:
            return {**data, "checks_state": CHECKS_READ}
        return {
            **data,
            "checks_state": CHECKS_UNREAD + (data.get("runs_why") or NO_CHECK_READ),
        }

    @model_validator(mode="after")
    def _checks_state_is_one_of_four(self) -> ModelSweepOpenPr:
        state = self.checks_state
        if state == CHECKS_READ:
            if self.runs is None:
                raise ValueError(
                    f"checks_state read for {self.repo}#{self.number} carries no runs"
                )
        elif state != CHECKS_ROLLUP_ONLY and not (
            state.startswith((CHECKS_SKIPPED, CHECKS_UNREAD))
            and state.partition(":")[2].strip()
        ):
            raise ValueError(
                f"checks_state {state!r} of {self.repo}#{self.number} is not one of "
                "read, rollup-only, skipped:<why>, unread:<why>"
            )
        return self


class ModelSweepMerge(BaseModel):
    """A merge the watcher followed. ``files`` is ``None`` when its changed files were not read."""

    model_config = _FROZEN

    repo: str
    number: int
    merged_at: str | None = None
    files: list[str] | None = None


class ModelMergeSweepReadRequest(BaseModel):
    """Everything one reading needs, gathered; the time and the host load are arguments.

    The repositories arrive as data, never from code: ``fleet`` names every repository a sweep
    reads, ``non_product_repos`` those whose merges are never product merges, and ``out_of_scope``
    maps each repository a ruling put out of the sweep to that ruling. An out-of-scope repository's
    open PRs are named under ``out_of_scope_prs`` and never read or classified. A merge to a
    repository in neither ``fleet`` nor ``non_product_repos`` is not counted. Every red is
    classified, and PRs are read in (repository, number) order whatever order they arrive in.
    """

    model_config = _FROZEN

    now: UtcStamp
    window_min: int = 60
    fleet: list[str] = Field(min_length=1)
    non_product_repos: list[str] = Field(default_factory=list)
    out_of_scope: dict[str, str] = Field(default_factory=dict)
    load1: float | None = None
    cpus: int | None = None
    floors: dict[str, float] = Field(default_factory=dict)
    open_prs: list[ModelSweepOpenPr] = Field(default_factory=list)
    merges: list[ModelSweepMerge] = Field(default_factory=list)
    ledger_lines: list[str] = Field(default_factory=list)
    ticks: list[dict[str, Any]] | None = None

    @field_validator("fleet", "non_product_repos")
    @classmethod
    def _each_repository_is_named(cls, value: list[str]) -> list[str]:
        if any(not repo.strip() for repo in value):
            raise ValueError("a repository list names an empty repository")
        return value

    @field_validator("out_of_scope")
    @classmethod
    def _each_ruled_out_repo_names_its_ruling(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        for repo, ruling in value.items():
            if not repo.strip() or not ruling.strip():
                raise ValueError(
                    f"out_of_scope entry {repo!r} names no ruling: a repository leaves the "
                    "sweep only by a ruling"
                )
        return value


class ModelMergeSweepUnread(BaseModel):
    """A fact the reading could not read, and why."""

    model_config = _FROZEN

    field: str
    why: str


class ModelMergeSweepReadResult(BaseModel):
    """One reading of the fleet, in the shape the lane plan consumes.

    ``scope`` is the fleet less its out-of-scope repositories: the repositories a lane may work in.
    """

    model_config = _FROZEN

    now: str
    load1: float | None
    cpus: int | None
    product: dict[str, Any]
    controller: dict[str, Any]
    reds: list[dict[str, Any]]
    chain_heads: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    open_prs: int
    scope: list[str] = Field(default_factory=list)
    out_of_scope_prs: list[str] = Field(default_factory=list)
    unread: list[ModelMergeSweepUnread]


class ModelMergeSweepClaimCheckRequest(BaseModel):
    """The claim-time recheck of one PR: its state in the watcher state and the ledger rows.

    ``self_lane`` is the asking lane: its own CLAIM is not an owner; any other live lane's is.
    """

    model_config = _FROZEN

    pr: str
    now: UtcStamp
    state: str | None = None
    title: str | None = None
    state_why: str = ""
    self_lane: str | None = None
    ledger_lines: list[str] = Field(default_factory=list)


class ModelMergeSweepClaimCheckResult(BaseModel):
    """FREE or STALE exits 0, a live owner 2, merged or closed 3, an unread state 4."""

    model_config = _FROZEN

    key: str
    state: str
    owner_state: str | None
    lane: str | None
    verdict: str
    exit_code: int
    line: str
