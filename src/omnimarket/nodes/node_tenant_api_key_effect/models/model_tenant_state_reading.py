# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tenant's provisioning state and the database's own clock, read in one statement."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelTenantStateReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provisioning_state: str | None
    database_now: datetime


__all__: list[str] = ["ModelTenantStateReading"]
