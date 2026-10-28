# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts supplied by a lane or worker producer; no dispatcher owns the ceiling."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneModelPolicyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    requested_engine: str = Field(default="auto", min_length=1)
    requested_tier: str | None = None
    band: str | None = None
    lane_kind: str = Field(min_length=1)
    risk_class: str | None = None
    route_ceiling: str | None = None
    required_engine: str | None = None
    required_model: str | None = None
    available_models: tuple[str, ...] | None = None
    available_engines: tuple[str, ...] | None = None
    code_edit_ready: bool = False
    # complexity:<band B4 or above> or sonnet-failed:<run or lane>; Opus needs one.
    escalation_reason: str | None = None
