# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing red class of one head."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_landing_red_class import (
    EnumLandingRedClass,
)


class ModelLandingRedVerdict(BaseModel):
    """``worker_reds`` are the given red names without the hostile-review family: a worker's brief lists only the reds
    it can fix (OMN-20067)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    red_class: EnumLandingRedClass
    worker_reds: tuple[str, ...]


__all__: list[str] = ["ModelLandingRedVerdict"]
