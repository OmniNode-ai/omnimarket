# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the install-proof read (OMN-21815): the facts the caller read and the verdict line."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_verdict import (
    ModelInstallChangedFile,
)


class ModelInstallComment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    login: str = ""
    body: str = ""


class ModelInstallProofReadRequest(BaseModel):
    """The PR's facts. ``clone`` is the repository's canonical clone, resolved by the caller; ``ledger_path`` the
    ledger file the STATUS rows are read from (empty: none). ``write_logins`` are the logins the PR write node
    comments under: a comment counts as the PR's own when its author wrote the PR or is one of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    head: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    lane: str = ""
    ticket: str = ""
    pr_body: str = ""
    pr_author: str = ""
    lab_exempt_kind: str = ""
    files: tuple[ModelInstallChangedFile, ...] = ()
    comments: tuple[ModelInstallComment, ...] = ()
    write_logins: tuple[str, ...] = ()
    ledger_path: str = ""
    clone: str = Field(min_length=1)


class ModelInstallProofReadResult(BaseModel):
    """``line`` is what the frozen plugin parses: it starts ``OK``, ``NA`` or ``NO``. An unreadable declaration is a
    ``NO`` with ``refused`` true, because the plugin passes any other first word."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["OK", "NA", "NO"]
    detail: str
    refused: bool = False
    required_files: tuple[str, ...] = ()

    @property
    def line(self) -> str:
        return f"{self.status} {self.detail}"
