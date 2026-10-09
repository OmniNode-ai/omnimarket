# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared red-CI observations, classification facts and sweep decisions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_GITHUB_OWNER = "OmniNode-ai"

ISO_Z_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"


def ci_red_repo_slug(repo: str) -> str:
    return repo if "/" in repo else f"{DEFAULT_GITHUB_OWNER}/{repo}"


def ci_run_failed_event_id(
    repo: str, pr_number: int, head_sha: str, checks: Iterable[str]
) -> str:
    identity = f"{repo}\n{pr_number}\n{head_sha}\n" + "\n".join(sorted(set(checks)))
    return hashlib.sha256(identity.encode()).hexdigest()


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
        return self


class ModelCiRedFacts(BaseModel):
    """Facts shared by the reader and pure classifier; absent reads stay explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event: ModelCiRunFailedEvent
    check_conclusions: dict[str, str] = Field(default_factory=dict)
    base_red_checks: tuple[str, ...] = ()
    base_read: bool = False


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
        if self.correlation_id != uuid5(
            NAMESPACE_URL, "onex:ci-red-decision:" + self.decision_key
        ):
            raise ValueError("correlation_id does not match decision_key")
        if self.repo != ci_red_repo_slug(self.repo) or "/" not in self.repo:
            raise ValueError("decision repo must be a full slug")
        if self.initial_state != f"ci_red:{self.red_class}":
            raise ValueError("initial_state does not match red_class")
        datetime.fromisoformat(self.observed_at)
        return self
