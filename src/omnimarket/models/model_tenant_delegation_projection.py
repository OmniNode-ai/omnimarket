# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rows read from the delegation and savings projections, and the HTTP answer shape."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTenantSavingsAggregate(BaseModel):
    """One tenant's savings headline as the projections hold it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_saved_usd: float = 0.0
    total_spend_usd: float = 0.0
    delegation_runs: int = 0
    savings_latest: datetime | None = None
    runs_latest: datetime | None = None


class ModelDelegationRunRow(BaseModel):
    """One delegation run row of a tenant, newest first when listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = ""
    task_type: str = ""
    model: str = ""
    quality_gate_passed: bool = False
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    occurred_at: datetime | None = None


class ModelTenantReadRequest(BaseModel):
    """The tenant a read is scoped to; it comes from the resolved identity only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    limit: int = Field(default=5, ge=1, le=50)


class ModelTenantReadHttpResponse(BaseModel):
    """A status and a JSON body, as the original route answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: dict[str, Any]


__all__: list[str] = [
    "ModelDelegationRunRow",
    "ModelTenantReadHttpResponse",
    "ModelTenantReadRequest",
    "ModelTenantSavingsAggregate",
]
