# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the handshake policy gate decisions (OMN-20671)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumPolicyGateDecisionKind(StrEnum):
    PARSE_REPOS = "parse_repos"
    RESOLVE_BRANCH = "resolve_branch"
    CLASSIFY_READ = "classify_read"
    REPORT = "report"


class EnumRepoGateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NO_WORKFLOW = "no_workflow"
    NO_RUNS = "no_runs"
    ERROR = "error"


class EnumReadOutcome(StrEnum):
    """Result of one read of a repo's latest completed handshake run."""

    PASS = "pass"
    FAIL = "fail"
    NO_WORKFLOW = "no_workflow"
    NO_RUNS = "no_runs"
    ERROR = "error"
    RETRY = "retry"


class EnumPolicyGateVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


class ModelRepoGateStatus(BaseModel):
    """One repo's final status, in the order the repos were checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    status: EnumRepoGateStatus


class ModelPolicyGateDecisionRequest(BaseModel):
    """One decision of the policy gate; the caller performs every read and passes what it saw."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    kind: EnumPolicyGateDecisionKind
    repos_conf_text: str | None = None
    repo: str | None = Field(default=None, min_length=1)
    default_branch_output: str | None = None
    api_ok: bool | None = None
    api_error_text: str = ""
    total_count: int | None = Field(default=None, ge=0)
    conclusion: str = ""
    attempt: int = Field(default=1, ge=1)
    max_attempts_raw: str | None = None
    base_delay_raw: str | None = None
    statuses: list[ModelRepoGateStatus] | None = Field(default=None, min_length=1)
    strict: bool = False

    @model_validator(mode="after")
    def validate_fields_for_kind(self) -> Self:
        kind = self.kind
        required: dict[EnumPolicyGateDecisionKind, tuple[str, ...]] = {
            EnumPolicyGateDecisionKind.PARSE_REPOS: ("repos_conf_text",),
            EnumPolicyGateDecisionKind.RESOLVE_BRANCH: (
                "repo",
                "default_branch_output",
            ),
            EnumPolicyGateDecisionKind.CLASSIFY_READ: ("repo", "api_ok"),
            EnumPolicyGateDecisionKind.REPORT: ("statuses",),
        }
        for field in required[kind]:
            if getattr(self, field) is None:
                raise ValueError(f"{kind.value} requires {field}")
        owned = {name for names in required.values() for name in names}
        owned |= {
            "api_error_text",
            "total_count",
            "conclusion",
            "attempt",
            "max_attempts_raw",
            "base_delay_raw",
            "strict",
        }
        allowed = set(required[kind])
        if kind is EnumPolicyGateDecisionKind.CLASSIFY_READ:
            allowed |= {
                "api_error_text",
                "total_count",
                "conclusion",
                "attempt",
                "max_attempts_raw",
                "base_delay_raw",
            }
        if kind is EnumPolicyGateDecisionKind.REPORT:
            allowed |= {"strict"}
        defaults = type(self).model_fields
        for field in sorted(owned - allowed):
            if getattr(self, field) != defaults[field].default:
                raise ValueError(f"{kind.value} does not take {field}")
        return self


class ModelPolicyGateDecisionResult(BaseModel):
    """What to do next; no field is set that the kind did not decide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumPolicyGateDecisionKind
    info_line: str | None = None
    repos: list[str] | None = None
    branch: str | None = None
    default_branch_endpoint: str | None = None
    runs_endpoint: str | None = None
    outcome: EnumReadOutcome | None = None
    next_attempt: int | None = None
    retry_delay_seconds: int | None = None
    report_text: str | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    failed_repos: list[str] | None = None
    verdict: EnumPolicyGateVerdict | None = None
    exit_code: int | None = None
