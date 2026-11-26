# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the tenant outbox flush: the command, one outbox row and the typed answer."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumFlushOutcome(StrEnum):
    """What one flush found and did."""

    FLUSHED = "flushed"
    NOTHING_PENDING = "nothing_pending"
    PUBLISH_FAILED = "publish_failed"
    STORE_UNAVAILABLE = "store_unavailable"


class ModelTenantOutboxFlushCommand(BaseModel):
    """Flush up to ``limit`` unpublished rows.

    Extra fields are ignored on purpose: the same model is the input of the route on the provision-completed event, whose
    payload names the tenant just written and is only the signal that a row waits.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


class ModelOutboxRow(BaseModel):
    """One unpublished ``tenant_event_outbox`` row: its id, destination topic and envelope."""

    model_config = ConfigDict(frozen=True)

    id: int
    topic: str
    envelope: dict[str, Any]


class ModelTenantOutboxFlushResult(BaseModel):
    """The typed answer of one flush."""

    model_config = ConfigDict(frozen=True)

    outcome: EnumFlushOutcome
    published: int = 0
    failed: int = 0
    detail: str = ""


__all__ = [
    "EnumFlushOutcome",
    "ModelOutboxRow",
    "ModelTenantOutboxFlushCommand",
    "ModelTenantOutboxFlushResult",
]
