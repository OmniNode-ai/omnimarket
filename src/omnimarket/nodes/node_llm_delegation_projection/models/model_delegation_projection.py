# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Models for the LLM delegation projection reducer."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum, unique

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumFreshnessState(StrEnum):
    """Freshness state of a projection row."""

    FRESH = "FRESH"
    STALE = "STALE"
    REPLAYING = "REPLAYING"


REDUCER_VERSION = "1.0.0"


class ModelDelegationDailyAggregate(BaseModel):
    """One aggregate row for (date, task_type, model_id, model_tier)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_date: str = Field(..., description="ISO date string YYYY-MM-DD.")
    task_type: str
    model_id: str
    model_tier: str

    total_calls: int = Field(default=0, ge=0)
    successful_calls: int = Field(default=0, ge=0)
    escalated_calls: int = Field(default=0, ge=0)
    total_tokens_in: int = Field(default=0, ge=0)
    total_tokens_out: int = Field(default=0, ge=0)
    total_latency_ms: int = Field(default=0, ge=0)
    avg_latency_ms: Decimal = Field(default=Decimal("0"))

    total_actual_cost_usd: Decimal = Field(default=Decimal("0"))
    total_opus_equivalent_usd: Decimal = Field(default=Decimal("0"))
    total_savings_usd: Decimal = Field(default=Decimal("0"))

    avg_quality_score: float | None = Field(default=None)

    projection_cursor: str = Field(
        ..., description="topic:partition:offset of last applied event."
    )
    source_event_id: str
    source_topic: str
    source_partition: int
    source_offset: int
    freshness_state: EnumFreshnessState = Field(default=EnumFreshnessState.FRESH)
    reducer_version: str = Field(default=REDUCER_VERSION)
    idempotency_key: str = Field(
        ..., description="correlation_id:causation_id:terminal_event_id"
    )


class ModelDelegationProjectionResult(BaseModel):
    """Result returned from a single handler invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    idempotency_key: str
    projection_cursor: str
    skipped_duplicate: bool = Field(default=False)


__all__: list[str] = [
    "REDUCER_VERSION",
    "EnumFreshnessState",
    "ModelDelegationDailyAggregate",
    "ModelDelegationProjectionResult",
]
