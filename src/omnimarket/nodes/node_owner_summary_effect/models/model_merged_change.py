# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One merged change from the PR watcher's local state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelMergedChange(BaseModel):
    """A merged pull request; the author is deliberately not carried."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    title: str
    merged_at: str
    tickets: tuple[str, ...] = ()
