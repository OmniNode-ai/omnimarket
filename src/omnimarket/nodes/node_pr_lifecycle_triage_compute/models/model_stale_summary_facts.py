# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher ci block at a head, for the stale CI Summary test."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_ci import (
    ModelWatcherCi,
)


class ModelStaleSummaryFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ci: ModelWatcherCi
    head: str


__all__: list[str] = ["ModelStaleSummaryFacts"]
