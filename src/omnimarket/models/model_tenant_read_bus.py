# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus command and terminal events the two tenant read nodes answer on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumTenantReadKind(StrEnum):
    """Which tenant read a bus command asks for."""

    SAVINGS = "savings"
    DELEGATIONS = "delegations"


class ModelTenantReadCommand(BaseModel):
    """A bus command for one tenant read; the handler answers with a terminal event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tenant_id: UUID
    kind: EnumTenantReadKind
    limit: int = Field(default=5, ge=1, le=50)


class ModelTenantReadCompleted(BaseModel):
    """The bus terminal event a tenant read answers with on success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    kind: EnumTenantReadKind
    tenant_id: UUID
    body: dict[str, Any]


class ModelTenantReadFailed(BaseModel):
    """The bus terminal event a tenant read answers with on failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    kind: EnumTenantReadKind
    tenant_id: UUID | None
    reason: str = Field(min_length=1)
    detail: str


#: The delegation projection's ``data_source`` column is the source of a run's
#: traffic class. A known value maps to the canonical class; an unknown value is
#: kept as it is, so a new source never drops a run from the answer.
TRAFFIC_CLASS_BY_DATA_SOURCE: Mapping[str, str] = {
    "real": "organic",
    "fixture": "fixture",
    "experiment": "synthetic",
}
