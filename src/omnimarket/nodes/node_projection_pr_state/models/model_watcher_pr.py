# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schema-1 watcher PR entry."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_pr_state.models.model_watcher_ci import (
    ModelWatcherCi,
)
from omnimarket.nodes.node_projection_pr_state.models.model_watcher_pr_facts import (
    ModelWatcherPrFacts,
)


class ModelWatcherPr(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    facts: ModelWatcherPrFacts
    ci: ModelWatcherCi | None
    watcher_class: str = Field(alias="cls")
    queued: bool
