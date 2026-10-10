# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The next stale-summary action for the head."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_stale_step import (
    EnumStaleStep,
)


class ModelStaleStepVerdict(BaseModel):
    """``step`` None: the node decides (a worker next)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: EnumStaleStep | None = None


__all__: list[str] = ["ModelStaleStepVerdict"]
