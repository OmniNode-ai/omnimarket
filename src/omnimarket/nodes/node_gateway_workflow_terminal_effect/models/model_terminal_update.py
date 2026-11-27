# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The validated terminal fields the store writes in one statement."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelTerminalUpdate(BaseModel):
    """Every column the terminal writes, already validated and bounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # None: a publisher-malformed command's terminal names no tenant (OMN-26566); the
    # store then joins on the correlation id alone and refuses more than one row.
    tenant_id: UUID | None
    correlation_id: UUID
    status: Literal["completed", "failed"]
    model_used: str
    total_tokens: int
    latency_ms: int
    route: str | None = None
    provider: str | None = None
    credential_source: Literal["customer_key", "house", "none"] | None = None
    content: str | None = None
    failure_reason: str | None = None
    failure_class: str | None = None
    failure_code: str | None = None
    rule_evaluations: list[Any]


__all__: list[str] = ["ModelTerminalUpdate"]
