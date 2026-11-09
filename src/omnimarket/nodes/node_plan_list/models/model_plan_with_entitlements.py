# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire shape of one plan and its entitlements."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ModelPlanEntitlement(BaseModel):
    """One metric limit of a plan."""

    metric_key: str
    window_key: str
    limit_value: int
    hard_cap: bool
    unit: str | None = None


class ModelPlanWithEntitlements(BaseModel):
    """A plan tier and its entitlements, in the field order the public API answers."""

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    is_default: bool
    created_at: datetime
    entitlements: list[ModelPlanEntitlement] = Field(default_factory=list)


__all__: list[str] = ["ModelPlanEntitlement", "ModelPlanWithEntitlements"]
