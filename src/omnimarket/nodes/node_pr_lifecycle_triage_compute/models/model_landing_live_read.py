# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live GitHub read of one PR's head, the fields the landing red rules read."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLandingLiveRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    rollup: str | None = None
    mergeable: str | None = None
    merge_state: str | None = None
    head: str | None = None


__all__: list[str] = ["ModelLandingLiveRead"]
