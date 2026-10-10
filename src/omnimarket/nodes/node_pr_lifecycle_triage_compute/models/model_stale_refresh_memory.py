# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The controller's per-PR ``stale_refresh`` memory: steps taken per head and update-branches spent."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelStaleRefreshMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    heads: dict[str, tuple[str, ...]] | None = None
    refreshes: int | None = None


__all__: list[str] = ["ModelStaleRefreshMemory"]
