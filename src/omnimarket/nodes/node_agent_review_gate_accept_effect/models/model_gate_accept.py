# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, policy, ports' data and decision of the gate-accept node."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumGateAcceptDecision(StrEnum):
    """The one decision the Hostile Review job reads off the node."""

    UNCHANGED = "unchanged"
    PASS_AGENT = "pass_agent"
    REFUSE = "refuse"


class EnumGateAcceptReason(StrEnum):
    """Why the node decided as it did; every decision carries exactly one."""

    RECEIPT_ACCEPTED = "receipt_accepted"
    VOTERS_PASSED = "voters_passed"
    CI_BLOCKED = "ci_blocked"
    BLOCKING_FINDING_FROM_SUCCEEDED_VOTER = "blocking_finding_from_succeeded_voter"
    UNEXPLAINED_VOTER_FAILURE = "unexplained_voter_failure"
    NO_UNSUCCESSFUL_VOTER = "no_unsuccessful_voter"
    NO_RECEIPT_REVIEW = "no_receipt_review"
    VOTER_RESULT_UNREADABLE = "voter_result_unreadable"
    REVIEWS_UNREADABLE = "reviews_unreadable"
    RECEIPT_MALFORMED = "receipt_malformed"
    RECEIPT_REFUSED = "receipt_refused"
    VERIFIER_UNREACHABLE = "verifier_unreachable"
    POLICY_UNAVAILABLE = "policy_unavailable"


class EnumVoterCause(StrEnum):
    """The typed cause for which a voter did not vote."""

    CREDIT_EXHAUSTED = "credit_exhausted"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"


class EnumGateAcceptTransport(StrEnum):
    IN_MEMORY = "in_memory"
    REDPANDA = "redpanda"


def _compiles(patterns: tuple[str, ...]) -> tuple[str, ...]:
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid pattern {pattern!r}: {exc}") from None
    return patterns


class ModelGateAcceptCauses(BaseModel):
    """Anchored patterns of the rendered voter error, one list per typed cause.

    Matching is ordered ``credit_exhausted``, ``timeout``, ``unavailable``: the
    exhausted-credit reply is also a 503, so the more specific cause goes first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    credit_exhausted: tuple[str, ...] = Field(min_length=1)
    timeout: tuple[str, ...] = Field(min_length=1)
    unavailable: tuple[str, ...] = Field(min_length=1)

    @field_validator("credit_exhausted", "timeout", "unavailable")
    @classmethod
    def _valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _compiles(value)


class ModelGateAcceptPolicy(BaseModel):
    """Everything the node decides by that is data, not code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publisher_login: str = Field(
        min_length=1, description="The app identity whose reviews carry receipts."
    )
    verifier_endpoint: str = Field(
        min_length=1, description="Where the verifier node answers."
    )
    receipt_fence: str = Field(
        min_length=1, description="The fenced block name that holds a receipt."
    )
    blocking_severities: tuple[str, ...] = Field(min_length=1)
    causes: ModelGateAcceptCauses


class ModelGateAcceptRequest(BaseModel):
    """One gate attempt: the pull request and where this attempt left its files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pull_request: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    voter_result_path: str = Field(min_length=1)
    step_output_path: str = Field(min_length=1)
    step_summary_path: str = Field(min_length=1)


class ModelGateAcceptResult(BaseModel):
    """The declared output: the decision, its typed reason, the receipt and a line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: EnumGateAcceptDecision
    reason: EnumGateAcceptReason
    receipt_id: str = ""
    summary_line: str = Field(min_length=1)


class ModelVoterStatus(BaseModel):
    """One voter of this attempt, read from the voter result file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    voter: str
    succeeded: bool
    blocking: bool = False
    cause: EnumVoterCause | None = None


class ModelReviewView(BaseModel):
    """One pull request review: who posted it, on which commit, and its text."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    author_login: str
    commit_id: str
    state: str
    body: str


class ModelReceiptVerifyRequest(BaseModel):
    """What the verifier is asked: one receipt against the head the gate runs on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: dict[str, Any]
    repository: str
    pull_request: int
    head_sha: str
    verifier_endpoint: str


class ModelReceiptVerifyOutcome(BaseModel):
    """The verifier's answer: accepted with the receipt id, or a typed refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    receipt_id: str = ""
    reason: str = ""


class ModelGateAcceptTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    decided: str
