# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire record of the configuration one delegation attempt was sent with."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_context_strategy import (
    EnumDelegationContextStrategy,
)


class ModelAttemptConfiguration(BaseModel):
    """Call parameters and context strategy of one attempt (OMN-20168).

    A parameter is None when the dispatch did not set it, so the model or its
    inference profile default applied. Two attempts with the same backend and
    model and a different configuration are a configuration swap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    context_strategy_id: EnumDelegationContextStrategy = (
        EnumDelegationContextStrategy.FULL_CONTEXT
    )
