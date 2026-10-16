# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One item of one arm, as both judges and the call recorded it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelComparisonObservation(BaseModel):
    """Numeric and boolean facts of one judged item. No prompt or answer text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    accepted_primary: bool
    accepted_second: bool
    latency_ms: int | None = Field(
        default=None,
        ge=0,
        description="None when the call was batched and has no interactive latency.",
    )
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float = Field(
        ge=0.0,
        description="List price of a Claude call; measured or estimated cost of a local call.",
    )
