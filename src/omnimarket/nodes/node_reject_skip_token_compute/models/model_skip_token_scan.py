# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one skip-token scan.

The scan input is text the caller already read (staged blobs, a commit message, a PR body); the
node does no I/O, so the same models serve the pre-commit export, the CI wrapper and the bus.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSkipTokenSurface(StrEnum):
    """Where the scanned text came from; only a staged file is filtered by its path."""

    STAGED_FILE = "staged_file"
    COMMIT_MESSAGE = "commit_message"
    PR_BODY = "pr_body"


class EnumSkipTokenVerdict(StrEnum):
    PASS = "pass"
    BLOCK = "block"


class ModelSkipTokenScanItem(BaseModel):
    """One text to scan: its surface, a label naming it (the repo-relative path for a staged file) and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: EnumSkipTokenSurface
    path: str = Field(
        description="Repo-relative path for a staged file; a label otherwise."
    )
    text: str


class ModelSkipTokenScanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelSkipTokenScanItem, ...] = ()


class ModelSkipTokenFinding(BaseModel):
    """One scanned text that carries a ``[skip-<letter>`` token; ``allowed`` when it also carries an approval receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: EnumSkipTokenSurface
    path: str
    allowed: bool


class ModelSkipTokenScanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumSkipTokenVerdict
    scanned: int = Field(description="Items whose text was scanned.")
    out_of_scope: tuple[str, ...] = Field(
        default=(), description="Paths of staged files whose type is not scanned."
    )
    findings: tuple[ModelSkipTokenFinding, ...] = ()
