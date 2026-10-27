# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The allowed runner model and engine, or an explicit refusal."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLaneModelPolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed: bool
    model: str | None = None
    engine: str | None = None
    ceiling: str
    reason: str
    policy_source: str
