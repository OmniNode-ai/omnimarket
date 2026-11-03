# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the agent review dispatch: the request, the overlay-supplied config, the
lab review a voter returns, the receipt the signer issues, and the one terminal.

The receipt models mirror ``hostile_review_agent_receipt.v1``, the variant the signer
node of omniintelligence issues; this package keeps its own copy of the shape because a
node never imports another repository's models. Every voter, endpoint, threshold and
deadline arrives through ``ModelAgentReviewConfig``, which the node's overlay supplies.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SHA = r"^[0-9a-f]{40}$"


class EnumAgentReviewOutcome(StrEnum):
    """The three terminal events of one command."""

    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


class EnumAgentReviewReason(StrEnum):
    """Why a command ended refused or failed."""

    STALE_HEAD = "stale_head"
    LAB_RUNG_INSUFFICIENT = "lab_rung_insufficient"
    ROSTER_FORBIDDEN = "roster_forbidden"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PORT_ERROR = "port_error"
    POST_FAILED = "post_failed"


class ModelAgentVoter(BaseModel):
    """One voter of the roster: the identity a lab call must prove it reached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = ""
    provider: str = ""
    rung: str = ""
    endpoint_class: str = ""
    endpoint: str = ""
    model: str = ""
    route_key: str = ""


class ModelAgentReviewPolicy(BaseModel):
    """Which roster identities may be called and which classes never are."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approved_identities: tuple[str, ...]
    approved_rungs: tuple[str, ...]
    forbidden_providers: tuple[str, ...] = ()
    forbidden_endpoint_classes: tuple[str, ...] = ()
    forbidden_endpoints: tuple[str, ...] = ()
    forbidden_hosts: tuple[str, ...] = ()
    publisher_identity: str = Field(min_length=1)


class ModelAgentReviewQuorum(BaseModel):
    """The bar: the CI gate's own numbers, supplied by the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quorum_size: int = Field(ge=2)
    min_agreeing_voters: int = Field(ge=2)
    line_proximity_lines: int = Field(ge=0)
    blocking_severities: tuple[str, ...] = Field(min_length=1)


class ModelAgentHarnessRung(BaseModel):
    """One harness rung: the CLI harness a roster rung runs through, and what its receipt
    must show. The harness rung node reads every value from here; none is in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str = Field(min_length=1)
    login: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    call_budget_seconds: float = Field(gt=0)
    harness_script: str = Field(min_length=1)
    run_dir_leaf: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")


class ModelAgentSigningKeyStore(BaseModel):
    """Where the receipt signing key lives in the secret store: a reference, never a value.

    One ``secret://<provider>/<environment>/<path...>/<name>`` reference, the form the
    private overlay holds every credential in. The dispatch resolves it at bind time through
    the host's own store identity, so the key is held in this process's memory and handed to
    the signer child, and is never a file, an argument or a setting on any host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(
        pattern=r"^secret://[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){2,}$"
    )

    @property
    def _parts(self) -> tuple[str, str, list[str], str]:
        provider, environment, *rest = self.reference.removeprefix("secret://").split(
            "/"
        )
        *path, name = rest
        return provider, environment, path, name

    @property
    def provider(self) -> str:
        provider, *_ = self._parts
        return provider

    @property
    def environment_slug(self) -> str:
        _, environment, *_ = self._parts
        return environment

    @property
    def secret_path(self) -> str:
        _, _, path, _ = self._parts
        return "/" + "".join(f"{part}/" for part in path)

    @property
    def secret_name(self) -> str:
        _, _, _, name = self._parts
        return name


class ModelAgentReviewConfig(BaseModel):
    """``<root>/node_agent_review_dispatch_orchestrator/overlay.yaml``; every key required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["in_memory", "redpanda"]
    overlay_revision: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1, max_length=63)
    receipt_fence: str = Field(min_length=1)
    deadline_seconds: float = Field(gt=0)
    lab_call_timeout_seconds: float = Field(gt=0)
    post_attempts: int = Field(ge=1)
    roster: tuple[ModelAgentVoter, ...] = Field(min_length=1)
    policy: ModelAgentReviewPolicy
    quorum: ModelAgentReviewQuorum
    #: The store reference of the receipt signing key; the signer binds to it.
    signing_key_store: ModelAgentSigningKeyStore
    #: Rung name -> harness, for every roster rung served by the harness rung node. A
    #: roster rung that is neither the lab rung nor a key here has no binding.
    harness_rungs: dict[str, ModelAgentHarnessRung] = Field(default_factory=dict)


class ModelAgentReviewRequest(BaseModel):
    """The command: one pull request head to review, delivered under one command id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(min_length=1)
    repo: str = Field(pattern=_REPO)
    pr: int = Field(gt=0)
    head_sha: str = Field(pattern=_SHA)


class ModelAgentFinding(BaseModel):
    """One finding a voter raised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: str = Field(min_length=1)
    claim: str = ""


class ModelAgentVoterReview(BaseModel):
    """What one lab call returned. ``delegation_event_id`` is empty when the call left none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    endpoint: str
    model: str
    delegation_event_id: str = ""
    findings: tuple[ModelAgentFinding, ...] = ()


class ModelAgentReceiptVoter(BaseModel):
    """A counted voter and the provenance of its lab call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    provider: str
    rung: str
    endpoint_class: str
    endpoint: str
    model: str
    delegation_event_id: str


class ModelAgentRosterSnapshot(BaseModel):
    """The voters the review was configured with and the overlay revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    voters: tuple[str, ...]
    overlay_revision: str


class ModelAgentReceiptCandidate(BaseModel):
    """The unsigned receipt: what the signer is asked to sign."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    repo: str = Field(pattern=_REPO)
    pr: int = Field(gt=0)
    head_sha: str = Field(pattern=_SHA)
    reviewer_id: str
    voters: tuple[ModelAgentReceiptVoter, ...]
    roster_snapshot: ModelAgentRosterSnapshot
    quorum_verdict: Literal["passed", "blocked"]
    blocking: tuple[str, ...] = ()
    findings_count: int = Field(ge=0)
    issued_at: datetime


class ModelAgentReceipt(ModelAgentReceiptCandidate):
    """The signed receipt: the candidate and its signature."""

    signature: str = Field(min_length=1)


class ModelAgentQuorumRequest(BaseModel):
    """The reviews the lab returned and the roster they were asked of."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    roster: tuple[ModelAgentVoter, ...]
    reviews: tuple[ModelAgentVoterReview, ...]
    quorum: ModelAgentReviewQuorum


class ModelAgentQuorumResult(BaseModel):
    """The counted voters and the verdict the CI gate's rule gives over them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counted: tuple[ModelAgentVoter, ...]
    delegation_event_ids: tuple[str, ...]
    sufficient: bool
    verdict: Literal["passed", "blocked"]
    blocking: tuple[str, ...] = ()
    findings_count: int = Field(ge=0)


class ModelAgentReviewTopics(BaseModel):
    """The command topic and the three terminal topics, as the contract declares them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    completed: str
    refused: str
    failed: str

    def terminal_topic(self, outcome: EnumAgentReviewOutcome) -> str:
        return {
            EnumAgentReviewOutcome.COMPLETED: self.completed,
            EnumAgentReviewOutcome.REFUSED: self.refused,
            EnumAgentReviewOutcome.FAILED: self.failed,
        }[outcome]


class ModelAgentReviewTerminal(BaseModel):
    """The one terminal of a command. ``historical`` marks a replay of a recorded one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str
    outcome: EnumAgentReviewOutcome
    reason: EnumAgentReviewReason | None = None
    receipt_id: str | None = None
    historical: bool = False


__all__ = [
    "EnumAgentReviewOutcome",
    "EnumAgentReviewReason",
    "ModelAgentFinding",
    "ModelAgentQuorumRequest",
    "ModelAgentQuorumResult",
    "ModelAgentReceipt",
    "ModelAgentReceiptCandidate",
    "ModelAgentReceiptVoter",
    "ModelAgentReviewConfig",
    "ModelAgentReviewPolicy",
    "ModelAgentReviewQuorum",
    "ModelAgentReviewRequest",
    "ModelAgentReviewTerminal",
    "ModelAgentReviewTopics",
    "ModelAgentRosterSnapshot",
    "ModelAgentVoter",
    "ModelAgentVoterReview",
]
