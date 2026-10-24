# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One first-parent commit on a canonical clone's default branch."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCloneCommit(BaseModel):
    """A commit the clone shows and the PR watcher's merge list does not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr: int | None = None
    tickets: tuple[str, ...] = ()
    subject: str
