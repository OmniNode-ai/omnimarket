# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red verdict on a landing branch head, and the one cause lane it asks for (OMN-20730).

A dev push run that goes red names no pull request, so the PR path (ci-run-failed) never sees
it. node_pr_state_emit_effect turns the webhook ingress's branch-head verdict into a
``dev-head-red`` event; node_pr_lifecycle_orchestrator asks for one cause lane per cause unless
one already owns it, and records every decision.

The cause's owner key is the PR path's dev_head owner key (``dev:<repo>:<branch>:<check>``,
node_pr_lifecycle_triage_compute), so a dev-head owner the PR path claimed owns the push red too.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.pr_state import ISO_Z_PATTERN
from omnimarket.models.ci_red_triage import ci_red_cause_key, ci_red_repo_slug
from omnimarket.models.github_check_run import RED_CHECK_CONCLUSIONS
from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord

_CAUSE_KEY_RE = re.compile(r"^cause:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):[0-9a-f]{12}$")


def dev_head_red_event_id(repo: str, branch: str, head_sha: str, check: str) -> str:
    """One red per repository, branch, head and check; the delivery is not part of it."""
    identity = f"{ci_red_repo_slug(repo)}\n{branch}\n{head_sha}\n{check}"
    return hashlib.sha256(identity.encode()).hexdigest()


def dev_head_owner_key(repo: str, branch: str, check: str) -> str:
    """The PR path's dev_head owner key (handler_classify_ci_red), so the two share one owner."""
    return f"dev:{ci_red_repo_slug(repo)}:{branch}:{check}"


def dev_cause_dispatch_key(owner_key: str, head_sha: str) -> str:
    """The request's identity: one cause lane per owner and head. Its receiver deduplicates it."""
    return hashlib.sha256(f"{owner_key}@{head_sha}".encode()).hexdigest()


def dev_cause_ref(repo: str, check: str) -> str:
    """The remote-lane runner's ``--cause`` value, ``<repo>:<check>``."""
    return f"{ci_red_repo_slug(repo).split('/', 1)[1]}:{check}"


def dev_cause_lane_name(repo: str, dispatch_key: str) -> str:
    short = ci_red_repo_slug(repo).split("/", 1)[1].lower().replace("_", "-")
    return f"devred-{short}-{dispatch_key[:8]}"


def dev_head_red_decision_correlation_id(decision_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "onex:dev-head-red-decision:" + decision_key)


def dev_cause_request_correlation_id(dispatch_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "onex:dev-cause-lane:" + dispatch_key)


class EnumDevHeadRedAction(StrEnum):
    DISPATCH_CAUSE_LANE = "dispatch_cause_lane"
    OWNED = "owned"
    WITHHELD = "withheld"


class ModelDevHeadRedEvent(BaseModel):
    """A red verdict on a landing branch head, published on onex.evt.omnimarket.dev-head-red.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo: str = Field(min_length=3, description="'<owner>/<repo>'.")
    branch: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    check: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    completed_at: str = Field(pattern=ISO_Z_PATTERN)
    source: Literal["webhook-branch-head"] = "webhook-branch-head"
    source_delivery: str = Field(min_length=1)

    @field_validator("conclusion")
    @classmethod
    def red_conclusion(cls, value: str) -> str:
        if value not in RED_CHECK_CONCLUSIONS:
            raise ValueError(f"conclusion {value!r} is not red")
        return value

    @model_validator(mode="after")
    def matching_event_id(self) -> Self:
        if self.repo != ci_red_repo_slug(self.repo) or "/" not in self.repo:
            raise ValueError("repo must be a full slug")
        datetime.fromisoformat(self.completed_at)
        if self.event_id != dev_head_red_event_id(
            self.repo, self.branch, self.head_sha, self.check
        ):
            raise ValueError("event_id does not match the dev-head red identity")
        return self


class ModelDevCauseLaneRequest(BaseModel):
    """Ask for one cause lane: the remote-lane runner's ``run --lane <lane> --cause <cause>``.

    Published on onex.cmd.omnimarket.dev-cause-lane-requested.v1. A redelivered or re-decided
    request carries the same ``dispatch_key``; its receiver starts at most one lane per key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    dispatch_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_key: str = Field(min_length=1)
    lane: str = Field(pattern=r"^devred-[a-z0-9.-]+-[0-9a-f]{8}$")
    repo: str = Field(min_length=3)
    branch: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    check: str = Field(min_length=1)
    cause: str = Field(min_length=3)
    model: Literal["opus"] = "opus"
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: str = Field(pattern=ISO_Z_PATTERN)

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        if self.owner_key != dev_head_owner_key(self.repo, self.branch, self.check):
            raise ValueError("owner_key does not match repo, branch and check")
        if self.dispatch_key != dev_cause_dispatch_key(self.owner_key, self.head_sha):
            raise ValueError("dispatch_key does not match owner_key and head_sha")
        if self.correlation_id != dev_cause_request_correlation_id(self.dispatch_key):
            raise ValueError("correlation_id does not match dispatch_key")
        if self.cause != dev_cause_ref(self.repo, self.check):
            raise ValueError("cause must be <repo>:<check>")
        return self


class ModelDevHeadRedDecided(BaseModel):
    """One decision per red, published on onex.evt.omnimarket.dev-head-red-decided.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    decision_key: str = Field(min_length=1)
    owner_key: str = Field(min_length=1)
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo: str
    branch: str
    head_sha: str
    check: str
    action: EnumDevHeadRedAction
    dispatch_key: str | None = None
    lane: str | None = None
    owner: str | None = None
    evidence: str
    observed_at: str = Field(pattern=ISO_Z_PATTERN)

    @model_validator(mode="after")
    def valid_decision(self) -> Self:
        if self.correlation_id != dev_head_red_decision_correlation_id(
            self.decision_key
        ):
            raise ValueError("correlation_id does not match decision_key")
        dispatched = self.action == EnumDevHeadRedAction.DISPATCH_CAUSE_LANE
        if dispatched != (self.dispatch_key is not None and self.lane is not None):
            raise ValueError("only a dispatch names its dispatch_key and lane")
        if (self.action == EnumDevHeadRedAction.OWNED) != (self.owner is not None):
            raise ValueError("only an owned decision names its owner")
        return self


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _cells(raw_row: str) -> list[tuple[str, str]]:
    out = []
    for part in raw_row.split("\n", 1)[0].split("|"):
        key, sep, value = part.strip().partition("=")
        if sep and key and " " not in key:
            out.append((key, value.strip()))
    return out


def claim_owns_cause(raw_row: str, *, repo: str, check: str) -> bool:
    """True when a CLAIM row names this cause in a ``cause=`` cell.

    A remote lane's cell is ``<repo>:<check>`` (owner optional, check compared case- and
    punctuation-blind). The landing controller's relay CLAIM is ``cause=<cause key>`` with
    ``check=<checks>``: its repository is the key's, and it owns the check when its ``check=``
    cell lists it or its key is the check-level key of the check.
    """
    short = ci_red_repo_slug(repo).split("/", 1)[1].lower()
    want = _norm(check)
    cells = _cells(raw_row)
    listed = {
        _norm(name)
        for key, value in cells
        if key == "check"
        for name in value.split(",")
        if name.strip()
    }
    for key, value in cells:
        if key != "cause" or not value:
            continue
        matched = _CAUSE_KEY_RE.match(value)
        if matched:
            if matched[1].split("/", 1)[1].lower() == short and (
                want in listed
                or value == ci_red_cause_key(ci_red_repo_slug(repo), check)
            ):
                return True
            continue
        cause_repo, sep, cause_check = value.partition(":")
        if (
            sep
            and cause_repo.rsplit("/", 1)[-1].lower() == short
            and _norm(cause_check) == want
        ):
            return True
    return False


def live_cause_owner(
    rows: Iterable[ModelWorkLedgerRowRecord],
    *,
    repo: str,
    check: str,
    now: datetime,
    live: timedelta,
) -> str | None:
    """The lane of an open CLAIM appended within ``live`` of ``now`` that names this cause.

    A CLAIM is closed by a TERMINAL of its lane at or after it. Rows after ``now`` are ignored.
    """
    rows = sorted(
        (r for r in rows if r.row_ts <= now), key=lambda r: (r.row_ts, r.row_id)
    )
    for claim in rows:
        if claim.row_type != "CLAIM" or not claim.row_lane:
            continue
        if now - claim.row_ts > live:
            continue
        if not claim_owns_cause(claim.raw_row, repo=repo, check=check):
            continue
        closed = any(
            r.row_type == "TERMINAL"
            and r.row_lane == claim.row_lane
            and r.row_ts >= claim.row_ts
            for r in rows
        )
        if not closed:
            return claim.row_lane
    return None
