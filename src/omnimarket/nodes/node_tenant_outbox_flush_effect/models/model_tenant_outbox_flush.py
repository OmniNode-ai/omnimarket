# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command, row and answer of the tenant outbox flush."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumTenantOutboxFlushOutcome(StrEnum):
    FLUSHED = "flushed"
    NOTHING_TO_FLUSH = "nothing_to_flush"
    PARTIAL = "partial"
    FAILED_STORE_UNAVAILABLE = "failed_store_unavailable"


class ModelTenantOutboxFlushCommand(BaseModel):
    """One flush pass: at most ``batch_limit`` unpublished rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    batch_limit: int = Field(default=100, ge=1, le=1000)


class ModelOutboxRow(BaseModel):
    """One unpublished outbox row as the store reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    envelope: dict[str, Any]
    attempts: int = Field(default=0, ge=0)


class ModelTenantOutboxFlushResult(BaseModel):
    """The typed answer: counts per fate, and the detail line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    outcome: EnumTenantOutboxFlushOutcome
    detail: str
    published: int = 0
    failed: int = 0
    mark_failed: int = 0
    emitted_at: datetime


__all__ = [
    "EnumTenantOutboxFlushOutcome",
    "ModelOutboxRow",
    "ModelTenantOutboxFlushCommand",
    "ModelTenantOutboxFlushResult",
]
