# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher ci block at a head, the required contexts and the tick's time."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_ci import (
    ModelWatcherCi,
)


class ModelPendingRequiredFacts(BaseModel):
    """``required`` None: the required set is unread, so every pending check counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ci: ModelWatcherCi
    head: str
    required: tuple[str, ...] | None = None
    now: str


__all__: list[str] = ["ModelPendingRequiredFacts"]
