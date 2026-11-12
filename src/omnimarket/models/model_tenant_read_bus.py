# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus command and terminal events the two tenant read nodes answer on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    #: The field the runtime's failure-terminal guard reads; always equals ``reason``.
    terminal_failure_cause: str | None = None
    detail: str

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data


#: The delegation projection's ``data_source`` column is the source of a run's
#: traffic class. A known value maps to the canonical class; an unknown value is
#: kept as it is, so a new source never drops a run from the answer.
TRAFFIC_CLASS_BY_DATA_SOURCE: Mapping[str, str] = {
    "real": "organic",
    "fixture": "fixture",
    "experiment": "synthetic",
}
