# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pilot's manifest: its bar, the bar's hash and when each was fixed."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)


class ModelPilotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pilot: str = Field(min_length=1)
    bar: tuple[ModelPilotBarCriterion, ...] = ()
    bar_hash: str | None = None
    frozen_at: datetime | None = None
    started_at: datetime
