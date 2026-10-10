# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watcher record's ``facts`` block, the keys the landing red rules read."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelWatcherFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    state: str | None = None
    head_sha: str | None = None
    evidence_companion: int | str | float | None = None
    merged_at: str | None = None


__all__: list[str] = ["ModelWatcherFacts"]
