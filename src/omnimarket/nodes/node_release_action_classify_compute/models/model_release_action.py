# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts in and decision out of the release classifier (OMN-21492, T-R1).

Every login, branch, tag shape and identity arrives in ``policy`` (the private
``release_gate`` overlay); the node names none. The grant facts are T-R2's output.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.change_source.model_change_history import EnumAccountType
from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    ModelReleaseGrantFacts,
)


class EnumReleaseEvent(StrEnum):
    """What happened. ``other`` is any event the gate has no shape for."""

    PULL_REQUEST = "pull_request"
    TAG_PUSH = "tag_push"
    PUBLISH_RUN = "publish_run"
    RELEASE_EVENT = "release_event"
    OTHER = "other"


class EnumReleaseKind(StrEnum):
    RELEASE_PR = "release_pr"
    VERSION_TAG = "version_tag"
    PACKAGE_PUBLISH = "package_publish"
    GITHUB_RELEASE = "github_release"
    TRAIN_RELEASE = "train_release"
    DEV_MERGE_TAG = "dev_merge_tag"
    NONE = "none"


class EnumReleaseDecision(StrEnum):
    ADMIT = "admit"
    REFUSE = "refuse"
    BLOCKED = "blocked"


class ModelReleaseActor(BaseModel):
    """Who acted. ``run_identity`` is the identity the run carries, not a name it chose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    login: str = Field(min_length=1)
    account_type: EnumAccountType
    run_identity: str | None = None


class ModelReleasePullRequestFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    title: str
    head_branch: str
    head_sha: str = Field(min_length=7)
    labels: tuple[str, ...] = ()


class ModelReleaseActionRequest(BaseModel):
    """One action to classify.

    ``ref`` is the tag for a tag push or a release event and the workflow for a
    publish run. ``changed_files`` is ``None`` when the changed files could not be
    read, which blocks a pull request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    event: EnumReleaseEvent
    actor: ModelReleaseActor
    policy: ModelReleaseGatePolicy
    ref: str = ""
    pull_request: ModelReleasePullRequestFacts | None = None
    changed_files: tuple[str, ...] | None = None
    grant: ModelReleaseGrantFacts | None = None

    @model_validator(mode="after")
    def _a_pull_request_event_carries_its_facts(self) -> Self:
        if self.event is EnumReleaseEvent.PULL_REQUEST and self.pull_request is None:
            raise ValueError("a pull_request event carries pull_request facts")
        return self


class ModelReleaseActionDecision(BaseModel):
    """The release kind and the decision with its reason.

    ``reason`` is a stable code; ``line`` is the printed form and names the kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReleaseKind
    decision: EnumReleaseDecision
    reason: str = Field(min_length=1)
    line: str = Field(min_length=1)
