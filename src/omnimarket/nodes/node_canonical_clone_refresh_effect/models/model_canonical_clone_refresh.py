# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh request a host derives from a burst of merge events, and its receipt
(OMN-20496). The merge event itself, ``ModelRepoMerged``, is shared from
``omnimarket.models.model_repo_merged`` (OMN-19998) and re-exported here."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.models.model_repo_merged import validate_git_sha as _sha
from omnimarket.models.model_repo_merged import validate_repo_slug as _repo


class EnumCanonicalCloneRefreshStatus(StrEnum):
    """One result per run, read from the canonical-clone sync engine's own output."""

    ADVANCED = "advanced"
    UP_TO_DATE = "up_to_date"
    REFUSED = "refused"
    FAILED = "failed"
    NO_CLONE = "no_clone"


class ModelCanonicalCloneRefreshRequest(BaseModel):
    """One sync of one repository's clones on this host, covering ``events`` merge events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    target_sha: str
    events: int = Field(ge=1)
    sources: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        return _repo(value)

    @field_validator("target_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        return _sha(value)


class ModelCanonicalCloneRefreshReceipt(BaseModel):
    """What one sync run did on one host, published on the receipt topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    repo: str
    status: EnumCanonicalCloneRefreshStatus
    target_sha: str
    before_sha: str = ""
    after_sha: str = ""
    events_coalesced: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    message: str = Field(default="", max_length=2000)


class ModelCanonicalCloneRefreshTopics(BaseModel):
    """The node's two topics, read from its contract and nowhere else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: str
    receipt: str


__all__ = [
    "EnumCanonicalCloneRefreshStatus",
    "ModelCanonicalCloneRefreshReceipt",
    "ModelCanonicalCloneRefreshRequest",
    "ModelCanonicalCloneRefreshTopics",
    "ModelRepoMerged",
]
