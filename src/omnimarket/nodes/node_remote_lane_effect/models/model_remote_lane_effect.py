# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the remote-lane effect node (OMN-20669)."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$"


class EnumRemoteLaneRefRefusal(StrEnum):
    """Why a lane may not be sent from this ref (OMN-19998); the caller refuses the send on any of these."""

    SHA_MALFORMED = "sha-malformed"
    READ_FAILED = "read-failed"
    NO_DEFAULT_BRANCH = "no-default-branch"
    DEFAULT_BRANCH_NOT_ON_REMOTE = "default-branch-not-on-remote"
    BRANCH_NOT_PUSHED = "branch-not-pushed"


class ModelRemoteLaneRefRequest(BaseModel):
    """The repository a lane builds from and the ref or sha the dispatch named.

    With neither ``ref`` nor ``sha`` the lane builds from the repository's landing
    branch, its default branch on the remote. The registry roots name where the
    repository's canonical clone may be; the caller reads them from its overlay.
    """

    model_config = _FROZEN

    repo: str = Field(pattern=NAME_PATTERN)
    owner: str = Field(pattern=NAME_PATTERN)
    ref: str | None = None
    sha: str | None = None
    omni_home: str | None = None
    omnibase_internal_home: str | None = None
    timeout_s: float = Field(default=60.0, gt=0)


class ModelRemoteLaneRefResult(BaseModel):
    """The landing branch read and the sha the lane builds from, or why not.

    ``remote`` is the clone directory or the public URL the read went through.
    ``landing_branch`` is the remote's default branch; it is read before send for every
    lane that names no sha, whether or not it named a branch (OMN-19998). ``refusal``
    is set whenever ``sha`` is None, and ``error`` says it in words.
    """

    model_config = _FROZEN

    sha: str | None
    landing_branch: str | None = None
    remote: str = ""
    error: str | None = None
    refusal: EnumRemoteLaneRefRefusal | None = None
