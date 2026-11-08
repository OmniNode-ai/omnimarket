# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measured engine usage, with explicit unknowns and optional work identity."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

type UsageCount = Annotated[int, Field(strict=True, ge=0)] | Literal["UNKNOWN"]
type UsageSeconds = (
    Annotated[float, Field(ge=0, allow_inf_nan=False)] | Literal["UNKNOWN"]
)


class ModelRemoteLaneReceiptUsage(BaseModel):
    """No missing measurement becomes a zero; unknown cost is absent on the wire.

    Codex input_tokens includes cached input; tokens_cached/cache_read are subsets,
    not additional input. Claude cache_write is cache_creation_input_tokens.
    Counters count assistant messages, tool-use blocks/command items, and identical
    calls repeated after a failed result. Engine num_turns is used only when the
    assistant message sequence was not supplied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str = "UNKNOWN"
    model: str = "UNKNOWN"
    tokens_in: UsageCount = "UNKNOWN"
    tokens_out: UsageCount = "UNKNOWN"
    tokens_cached: UsageCount = "UNKNOWN"
    cache_read: UsageCount = "UNKNOWN"
    cache_write: UsageCount = "UNKNOWN"
    turns: UsageCount = "UNKNOWN"
    tool_calls: UsageCount = "UNKNOWN"
    retries: UsageCount = "UNKNOWN"
    wall_seconds: UsageSeconds = "UNKNOWN"
    cost_basis: Literal["MEASURED", "UNKNOWN"] = "UNKNOWN"
    total_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    contract_id: str | None = None
    attempt_id: str | None = None

    @model_validator(mode="after")
    def cost_has_basis(self) -> ModelRemoteLaneReceiptUsage:
        if (self.cost_basis == "MEASURED") != (self.total_cost_usd is not None):
            raise ValueError(
                "MEASURED cost requires total_cost_usd; UNKNOWN cost must omit it"
            )
        return self

    @model_serializer(mode="wrap")
    def omit_unreported(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}
