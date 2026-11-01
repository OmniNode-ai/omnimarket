# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of node_release_grant_read_effect (OMN-21493, T-R2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumMergeMethod,
    ModelReleaseAuthorizationReceipt,
    ModelReleaseGrantFacts,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseEvent,
    ModelReleaseActionDecision,
    ModelReleaseActor,
    ModelReleasePullRequestFacts,
)


class ModelReadReleaseGrantRequest(BaseModel):
    """Read the grant of one change at its current head commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pull_request: int = Field(ge=1)
    head_sha: str = Field(min_length=7)
    boundary: EnumGateBoundary
    policy: ModelReleaseGatePolicy


class ModelRecordAuthorizationReceiptRequest(BaseModel):
    """At merge: record the receipt linking the granted head to the merge commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pull_request: int = Field(ge=1)
    head_sha: str = Field(min_length=7)
    merge_commit_sha: str = Field(min_length=7)
    merge_method: EnumMergeMethod
    policy: ModelReleaseGatePolicy


class ModelRecordAuthorizationReceiptResult(BaseModel):
    """``receipt`` is set only when a fresh read at the merge boundary is a grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    written: bool
    reason: str = Field(min_length=1)
    facts: ModelReleaseGrantFacts
    receipt: ModelReleaseAuthorizationReceipt | None = None


class ModelCheckReleaseGateRequest(BaseModel):
    """One action the required check, the merge step or the train's release step asks about.

    ``head_checkout`` is the pull request's head checkout on disk, if any. The gate
    reads nothing under it: the policy comes from the private overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    event: EnumReleaseEvent
    actor: ModelReleaseActor
    boundary: EnumGateBoundary
    ref: str = ""
    pull_request: ModelReleasePullRequestFacts | None = None
    changed_files: tuple[str, ...] | None = None
    head_checkout: str | None = None


class ModelCheckReleaseGateResult(BaseModel):
    """The classifier's decision, the grant facts behind it and the check's exit code.

    ``exit_code`` is 0 for admit, 1 for refuse and 2 for blocked: any other value than
    0 fails the required check, so an unreadable input fails closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelReleaseActionDecision
    grant: ModelReleaseGrantFacts | None = None
    exit_code: int = Field(ge=0, le=2)


__all__ = [
    "ModelCheckReleaseGateRequest",
    "ModelCheckReleaseGateResult",
    "ModelReadReleaseGrantRequest",
    "ModelRecordAuthorizationReceiptRequest",
    "ModelRecordAuthorizationReceiptResult",
]
