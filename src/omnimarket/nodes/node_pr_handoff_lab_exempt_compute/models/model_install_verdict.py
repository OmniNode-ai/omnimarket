# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the install-first verdict (OMN-21815)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_declaration import (
    ModelPrHandoffInstallDeclaration,
)


class ModelInstallSource(BaseModel):
    """One piece of text that may carry the proof: ``name`` is ``body``, ``comment`` or ``status``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["body", "comment", "status"]
    text: str


class ModelInstallChangedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str
    previous_filename: str | None = None


class ModelInstallAncestry(BaseModel):
    """What git said about one installed revision: ``yes`` it holds the live head, ``no`` it does not, ``unread``
    when it could not be read (never a pass)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["yes", "no", "unread"]
    why: str = ""


class ModelInstallVerdictRequest(BaseModel):
    """Every fact the verdict needs, read by the caller: the verdict itself is pure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    declaration: ModelPrHandoffInstallDeclaration
    repo: str = Field(min_length=1)
    head: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    #: the exempt kind handoff judged first (version-bump, pin-refresh, low-risk, cloud-ci), or empty.
    lab_exempt_kind: str = ""
    #: the PR body and the status rows of the lane or ticket: read before the changed files.
    sources: tuple[ModelInstallSource, ...] = ()
    #: the proof from the PR's conversation, already filtered to the author and the write node's logins.
    comment_sources: tuple[ModelInstallSource, ...] = ()
    files: tuple[ModelInstallChangedFile, ...] = ()
    #: installed revision (as named in a source) -> what git said about it.
    ancestry: dict[str, ModelInstallAncestry] = Field(default_factory=dict)


class ModelInstallVerdict(BaseModel):
    """``OK``: a proof names the live head installed and observed running. ``NA``: nothing here changes what runs on a
    host (or the PR is an exempt kind). ``NO``: it does, and the proof is missing or not enough. ``line`` is the one
    line the frozen plugin parses: ``<OK|NA|NO> <detail>``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["OK", "NA", "NO"]
    detail: str
    required_files: tuple[str, ...] = ()

    @property
    def line(self) -> str:
        return f"{self.status} {self.detail}"
