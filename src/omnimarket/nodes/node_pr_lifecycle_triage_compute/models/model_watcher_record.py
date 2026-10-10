# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One PR watcher record (``ci``, ``facts`` and the class the watcher gave it)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_ci import (
    ModelWatcherCi,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_facts import (
    ModelWatcherFacts,
)


class ModelWatcherRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    ci: ModelWatcherCi | None = None
    facts: ModelWatcherFacts | None = None
    cls: str | None = None


__all__: list[str] = ["ModelWatcherRecord"]
