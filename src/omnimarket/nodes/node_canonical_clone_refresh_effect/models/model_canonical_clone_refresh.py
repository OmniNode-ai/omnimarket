# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merge event, the refresh request a host derives from a burst of them, and its receipt
(OMN-20496)."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

ALLOWED_OWNER = "OmniNode-ai"
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _repo(value: str) -> str:
    if not _REPO_RE.match(value):
        raise ValueError(f"repo must be owner/name: {value!r}")
    if value.split("/", 1)[0] != ALLOWED_OWNER:
        raise ValueError(f"repo owner must be {ALLOWED_OWNER}: {value!r}")
    return value


def _sha(value: str) -> str:
    if not _SHA_RE.match(value):
        raise ValueError(f"sha must be 40 lowercase hex: {value!r}")
    return value


class EnumCanonicalCloneRefreshStatus(StrEnum):
    """One result per run, read from the canonical-clone sync engine's own output."""

    ADVANCED = "advanced"
    UP_TO_DATE = "up_to_date"
    REFUSED = "refused"
    FAILED = "failed"
    NO_CLONE = "no_clone"


class ModelRepoMerged(BaseModel):
    """A pull request merged into a repository branch: the event every host's clone follows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="owner/name on GitHub")
    base: str = Field(min_length=1, max_length=255)
    merge_sha: str
    pr_number: int = Field(ge=1)
    merged_at: Annotated[datetime, AwareDatetime]
    source: str = Field(min_length=1, max_length=64)
    state: Literal["merged", "open", "closed"] = "merged"

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        return _repo(value)

    @field_validator("merge_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        return _sha(value)


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
