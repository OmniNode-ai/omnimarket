# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order-edge predecessors' facts and when the head's red was last graded."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_facts import (
    ModelWatcherFacts,
)


class ModelFiredEdgeFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_states: dict[str, ModelWatcherFacts]
    last_red_completed_at: str | None = None


__all__: list[str] = ["ModelFiredEdgeFacts"]
