# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts for the brief gate: the brief, the dispatch, the watcher state and ``now``.

The mode is not a field: a request that carries one is refused by this model. A deployment's
mode and WIP policy are injected into the handler; ``config`` and ``policy`` here only
serve a caller that has no injected value, and can never loosen one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_config import (
    ModelLaneBriefPrGateConfig,
)
from omnimarket.models.wip_gate import (
    ModelWipExemption,
    ModelWipOverrideEvidence,
    ModelWipPolicy,
)


class ModelLaneBriefPrGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    brief: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    now: datetime
    state: dict[str, Any] | None = None
    config: ModelLaneBriefPrGateConfig | None = None
    policy: ModelWipPolicy | None = None
    exemption: ModelWipExemption | None = None
    authorized_swarm_lanes: tuple[str, ...] = ()
    override: ModelWipOverrideEvidence | None = None
    override_consumed_by: str | None = None
