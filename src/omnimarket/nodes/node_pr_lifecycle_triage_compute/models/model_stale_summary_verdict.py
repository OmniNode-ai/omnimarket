# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the head's red is only a CI Summary left red by cancelled or skipped siblings."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_stale_summary_kind import (
    EnumStaleSummaryKind,
)


class ModelStaleSummaryVerdict(BaseModel):
    """``kind`` None: the red is not provably a stale summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumStaleSummaryKind | None = None


__all__: list[str] = ["ModelStaleSummaryVerdict"]
