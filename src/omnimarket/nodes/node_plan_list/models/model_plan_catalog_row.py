# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the plan_tiers x plan_entitlements join, as the repository returns it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelPlanCatalogRow(BaseModel):
    """A plan tier with at most one of its entitlements (None when it has none)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    code: str
    name: str
    description: str | None
    is_default: bool
    created_at: datetime
    metric_key: str | None
    window_key: str | None
    limit_value: int | None
    hard_cap: bool | None
    unit: str | None


__all__: list[str] = ["ModelPlanCatalogRow"]
