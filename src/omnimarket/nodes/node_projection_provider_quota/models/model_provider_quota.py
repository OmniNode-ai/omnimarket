# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic projection input and the per-key row delta (OMN-20154)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.provider_quota import (
    EnumProviderQuotaOutcome,
    ModelProviderQuotaObserved,
)


class ModelProviderQuotaProjectionRequest(ModelProviderQuotaObserved):
    """Accept runtime metadata; never invent an ingest time."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _accept_envelope_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and "observed_at" not in data:
            return {**data, "observed_at": data.get("_envelope_timestamp")}
        return data


class ModelProviderQuotaRowDelta(BaseModel):
    """What one observation changes on one quota key.

    The writer merges it into the row in ONE statement, so the counters and the
    block are updated atomically and a concurrent observation can never be
    lost to a read-modify-write race.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    credential_ref: str
    provider_id: str
    model_scope: str
    observed_at: datetime
    window_seconds: int
    outcome: EnumProviderQuotaOutcome
    http_status: int | None
    provider_code: str | None
    counts_hit: bool
    sets_block: bool
    disposition: str | None
    blocked_until: datetime | None
    blocked_indefinitely: bool
    block_reason: str | None
    clears_blocks_before: datetime | None


class ModelProviderQuotaProjectionResult(BaseModel):
    """Row deltas derived by the pure fold, persisted by the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelProviderQuotaRowDelta, ...]
