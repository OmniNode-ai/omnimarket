# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared native gateway lifecycle and observed evidence; no prompt or key fields."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelClaudeGatewayConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    backend_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    credential_ref: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")
    bind_host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    bind_port: int = Field(default=8181, ge=1, le=65535)
    max_request_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)
    max_event_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    # Ordered native-Messages backends tried after backend_id, per request, when
    # the one before it is unreachable or overloaded (OMN-23413).
    fallback_backend_ids: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_ladder(self) -> Self:
        ladder = (self.backend_id, *self.fallback_backend_ids)
        if any(not item for item in ladder) or len(set(ladder)) != len(ladder):
            raise ValueError("Each gateway backend appears once in the ladder")
        return self


class ModelClaudeGatewayReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    model: str
    backend_id: str
    status: Literal["completed", "failed", "cancelled"]
    in_tokens: int | None = Field(default=None, ge=0)
    out_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cost_usd: Decimal | None = Field(default=None, ge=0)
    stop_reason: str | None = None
