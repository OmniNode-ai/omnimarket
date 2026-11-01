# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grant facts and the authorization receipt of the release gate (OMN-21493)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class EnumGrantState(StrEnum):
    GRANTED = "granted"
    NOT_GRANTED = "not_granted"
    BLOCKED = "blocked"


class EnumGateBoundary(StrEnum):
    """Where the gate was asked: the required check, the merge or the train's release."""

    CHECK = "check"
    MERGE = "merge"
    RELEASE = "release"


class EnumMergeMethod(StrEnum):
    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class ModelReleaseGrantFacts(BaseModel):
    """What the grant read established about one change at one head commit.

    ``applier_role`` names the role list the applier is in, never the login. A
    ``blocked`` state is an unreadable history, never an admission.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumGrantState
    reason: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    pull_request: int = Field(ge=1)
    head_sha: str = Field(min_length=7)
    boundary: EnumGateBoundary
    grant_event_id: str | None = None
    applier_role: str | None = None
    policy_digest: str = Field(min_length=1)
    read_at: AwareDatetime

    @model_validator(mode="after")
    def _a_grant_names_its_event_and_role(self) -> Self:
        if self.state is EnumGrantState.GRANTED and not (
            self.grant_event_id and self.applier_role
        ):
            raise ValueError("a granted state names the grant event and the role")
        return self


class ModelReleaseAuthorizationReceipt(BaseModel):
    """Written at merge: links the grant to the commit the merge produced.

    The train resolves a merge commit to the granted head through this receipt and
    re-reads the grant's revocation before it releases (T-R4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pull_request: int = Field(ge=1)
    head_sha: str = Field(min_length=7)
    merge_commit_sha: str = Field(min_length=7)
    merge_method: EnumMergeMethod
    grant_event_id: str = Field(min_length=1)
    policy_digest: str = Field(min_length=1)
    written_at: AwareDatetime
