# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One usage privilege: a quota per plan tier code and resource code."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelUsagePrivilege(BaseModel):
    """A row of the usage_privileges catalog, in the field order the public API answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID
    plan_tier_code: str
    resource_code: str
    monthly_quota: int | None = None
    hard_limit: bool
    created_at: datetime


__all__: list[str] = ["ModelUsagePrivilege"]
