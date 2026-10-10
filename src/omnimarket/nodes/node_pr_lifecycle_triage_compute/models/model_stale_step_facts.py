# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale summary's kind, the controller's memory of the PR and the head."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_stale_summary_kind import (
    EnumStaleSummaryKind,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_refresh_memory import (
    ModelStaleRefreshMemory,
)


class ModelStaleStepFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumStaleSummaryKind
    mem: ModelStaleRefreshMemory | None = None
    head: str


__all__: list[str] = ["ModelStaleStepFacts"]
