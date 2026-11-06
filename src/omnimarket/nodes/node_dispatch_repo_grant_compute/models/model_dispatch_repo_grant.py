# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result of the dispatch's repository grant: every repository a brief changes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

DEFAULT_OWNER = "OmniNode-ai"


class ModelDispatchRepoGrantRequest(BaseModel):
    """The brief, the dispatch's own repository and any repositories it already grants."""

    model_config = _FROZEN

    brief: str
    # The dispatch's --repo (``name`` or ``owner/name``); None for a lane sent with no repository.
    repo: str | None = Field(default=None, min_length=1)
    # Repositories the sender already grants beside ``repo`` (``name`` or ``owner/name``).
    granted: tuple[str, ...] = ()
    # The owner a bare repository name belongs to.
    owner: str = Field(default=DEFAULT_OWNER, min_length=1)


class ModelDispatchRepoTouch(BaseModel):
    """One repository the brief changes and the first brief text that says so."""

    model_config = _FROZEN

    repo: str
    evidence: str


class ModelDispatchRepoGrantResult(BaseModel):
    """The full grant to send the lane with, and what the brief added to the dispatch's own."""

    model_config = _FROZEN

    # ``owner/name``: the dispatch's repo, then its other grants, then the brief's repositories in brief order.
    grant: tuple[str, ...]
    # The repositories the brief changes that the dispatch did not name; the sender grants them too.
    ungranted: tuple[str, ...] = ()
    touched: tuple[ModelDispatchRepoTouch, ...] = ()
    # A receipt line naming each added repository and its evidence; None when nothing was added.
    receipt_line: str | None = None
