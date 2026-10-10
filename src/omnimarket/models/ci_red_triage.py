# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared red-CI observations, classification facts and sweep decisions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.pr_state import ISO_Z_PATTERN, ModelPrCheckFact

DEFAULT_GITHUB_OWNER = "OmniNode-ai"


def ci_red_repo_slug(repo: str) -> str:
    return repo if "/" in repo else f"{DEFAULT_GITHUB_OWNER}/{repo}"


def ci_run_failed_event_id(
    repo: str, pr_number: int, head_sha: str, checks: Iterable[str]
) -> str:
    identity = f"{repo}\n{pr_number}\n{head_sha}\n" + "\n".join(sorted(set(checks)))
    return hashlib.sha256(identity.encode()).hexdigest()


# Claim identities. The decision and owner claims are rows of the
# pr_lifecycle_ledger_entries projection keyed by these correlation ids, so the
# handler that reads a claim and the reducer that writes it share one spelling.
def ci_red_decision_correlation_id(decision_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "onex:ci-red-decision:" + decision_key)


def ci_red_owner_correlation_id(owner_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "onex:ci-red-owner:" + owner_key)


def ci_red_owner_run_id(owner_key: str) -> str:
    return "ci-red-" + hashlib.sha256(owner_key.encode()).hexdigest()[:16]


def ci_red_cause_key(slug: str, check: str) -> str:
    """Check-level cause key, used for a red whose annotations are unread."""
    return f"cause:{slug}:{hashlib.sha256(check.encode()).hexdigest()[:12]}"


# The owner claim is a lease. Its writer (node_pr_lifecycle_state_reducer) stamps
# the expiry, the row's next_check_at, from the TTL its contract declares; an owner
# read counts a claim only before that expiry. So an owner that never closes, as a
# lane whose TERMINAL the ledger refused, stops owning its PRs at the TTL.
def ci_red_owner_claim_ttl(block: object) -> timedelta:
    """The contract's ``owner_claim_lease`` block; a missing or unknown TTL is refused."""
    if not isinstance(block, dict) or set(block) != {"ttl_seconds"}:
        raise ValueError(
            "owner_claim_lease must declare exactly ttl_seconds, got "
            f"{sorted(block) if isinstance(block, dict) else type(block).__name__}"
        )
    ttl = block["ttl_seconds"]
    if type(ttl) is not int or ttl <= 0:
        raise ValueError(
            f"owner_claim_lease.ttl_seconds must be a positive integer, got {ttl!r}"
        )
    return timedelta(seconds=ttl)


def ci_red_claim_holds(lease_expires_at: object, now: datetime) -> bool:
    """True while an owner claim's lease (its row's next_check_at) has not expired.

    Raises ValueError for an expiry that is not a timezone-aware time: an unknown
    lease is unread, never a claim held or released.
    """
    expires = (
        lease_expires_at
        if isinstance(lease_expires_at, datetime)
        else datetime.fromisoformat(str(lease_expires_at))
    )
    if expires.tzinfo is None:
        raise ValueError(f"lease expiry has no timezone: {lease_expires_at!r}")
    return now < expires


class EnumCiRedClass(StrEnum):
    SHARED_CAUSE = "shared_cause"
    PR_OWN = "pr_own"
    DEV_HEAD = "dev_head"
    RUNNER = "runner"


class EnumCiRedAction(StrEnum):
    START_CAUSE_OWNER = "start_cause_owner"
    START_PR_FIX = "start_pr_fix"
    START_DEV_CAUSE = "start_dev_cause"
    RERUN_FAILED = "rerun_failed"
    JOINED_OWNER = "joined_owner"
    RECORD_ONLY = "record_only"


# Decisions that started an owner and so claim it for their members.
CI_RED_OWNER_START_ACTIONS = frozenset(
    {
        EnumCiRedAction.START_CAUSE_OWNER,
        EnumCiRedAction.START_PR_FIX,
        EnumCiRedAction.START_DEV_CAUSE,
        EnumCiRedAction.RERUN_FAILED,
    }
)


class ModelCiRedPeer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pr_number: int = Field(gt=0)
    head_sha: str
    armed: bool
    red_contexts: tuple[str, ...]


class ModelCiRunFailedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    head_sha: str
    base: str
    armed: bool
    queued: bool
    failing_checks: tuple[str, ...] = Field(min_length=1)
    ci_read_at: str
    observed_at: str = Field(pattern=ISO_Z_PATTERN)
    source_digest: str
    peers: tuple[ModelCiRedPeer, ...] = ()
    # Per-check facts (PR-state schema version 2 and webhook check runs). Empty when the source
    # observation predates them; the triage then reads the checks from GitHub.
    failing_runs: tuple[ModelPrCheckFact, ...] = ()
    base_red_checks: tuple[str, ...] = ()
    base_read: bool = False

    @field_validator("failing_checks")
    @classmethod
    def canonical_checks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(not check for check in value):
            raise ValueError("failing_checks must be sorted, unique and non-empty")
        return value

    @field_validator("observed_at")
    @classmethod
    def valid_observed_at(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def matching_event_id(self) -> Self:
        if self.event_id != ci_run_failed_event_id(
            self.repo, self.pr_number, self.head_sha, self.failing_checks
        ):
            raise ValueError("event_id does not match red CI identity")
        if not {run.check for run in self.failing_runs} <= set(self.failing_checks):
            raise ValueError("failing_runs must name failing_checks only")
        return self

    @property
    def carries_check_facts(self) -> bool:
        """True when every failing check has its conclusion on the event itself."""
        return {run.check for run in self.failing_runs} == set(self.failing_checks)


class ModelCiRedFacts(BaseModel):
    """Facts shared by the reader and pure classifier; absent reads stay explicit.

    ``annotations`` is the first failure annotation per failing check at the
    event head (``first_failure_annotations``), and ``peer_annotations`` the
    same per peer PR number at the peer's head; a check or peer absent is
    unread and ``""`` is a check read with no annotation. With
    ``annotations_read`` false the classifier clusters by check name only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    event: ModelCiRunFailedEvent
    check_conclusions: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    peer_annotations: dict[int, dict[str, str]] = Field(default_factory=dict)
    annotations_read: bool = False
    base_red_checks: tuple[str, ...] = ()
    base_read: bool = False


def facts_from_event(event: ModelCiRunFailedEvent) -> ModelCiRedFacts | None:
    """The classifier's facts as the event states them; None when it does not carry them all.

    Every failing check's conclusion and the base branch's read are both needed: with the base
    unread the dev-head class cannot be ruled in or out, which is a decision, not a default.
    """
    if not (event.carries_check_facts and event.base_read):
        return None
    return ModelCiRedFacts(
        event=event,
        check_conclusions={run.check: run.conclusion for run in event.failing_runs},
        base_red_checks=event.base_red_checks,
        base_read=event.base_read,
    )


class ModelCiRedClassification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    red_class: EnumCiRedClass
    check: str
    members: tuple[int, ...]
    owner_key: str
    cause_key: str | None = None
    reason: str


class ModelCiRedTriageDecided(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    decision_key: str
    owner_key: str
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo: str
    pr_number: int = Field(gt=0)
    head_sha: str
    check: str
    red_class: EnumCiRedClass
    action: EnumCiRedAction
    action_applied: bool
    orchestrator_run_id: str | None
    members: tuple[int, ...]
    initial_state: str
    evidence: str
    observed_at: str = Field(pattern=ISO_Z_PATTERN)

    @model_validator(mode="after")
    def valid_decision(self) -> Self:
        if self.correlation_id != ci_red_decision_correlation_id(self.decision_key):
            raise ValueError("correlation_id does not match decision_key")
        if self.repo != ci_red_repo_slug(self.repo) or "/" not in self.repo:
            raise ValueError("decision repo must be a full slug")
        if self.initial_state != f"ci_red:{self.red_class}":
            raise ValueError("initial_state does not match red_class")
        datetime.fromisoformat(self.observed_at)
        return self

    def claimed_members(self) -> tuple[int, ...]:
        """PRs whose owner this decision claimed: none unless a start was applied."""
        if self.action_applied and self.action in CI_RED_OWNER_START_ACTIONS:
            return self.members
        return ()
