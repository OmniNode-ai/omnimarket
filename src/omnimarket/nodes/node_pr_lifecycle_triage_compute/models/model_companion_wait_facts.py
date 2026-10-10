# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One red PR, its live read and the watcher records its change-control companion is read from."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_live_read import (
    ModelLandingLiveRead,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_record import (
    ModelWatcherRecord,
)


class ModelCompanionWaitFacts(BaseModel):
    """``records`` need hold only the PR's companion; the rule reads no other key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short: str
    rec: ModelWatcherRecord
    live: ModelLandingLiveRead | None = None
    records: dict[str, ModelWatcherRecord]
    annotations: dict[str, str | None] | None = None


__all__: list[str] = ["ModelCompanionWaitFacts"]
