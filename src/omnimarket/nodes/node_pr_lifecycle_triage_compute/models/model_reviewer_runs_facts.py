# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher records, to count the hostile-review runs in flight on open PRs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_watcher_record import (
    ModelWatcherRecord,
)


class ModelReviewerRunsFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    records: dict[str, ModelWatcherRecord]


__all__: list[str] = ["ModelReviewerRunsFacts"]
