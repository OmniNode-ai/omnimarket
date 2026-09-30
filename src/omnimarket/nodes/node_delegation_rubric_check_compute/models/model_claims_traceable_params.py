# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for claims_traceable."""

from pydantic import BaseModel, ConfigDict, Field


class ModelClaimsTraceableParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id_pattern: str = Field(min_length=1)
    min_quote_words: int = Field(ge=1)
    min_number_digits: int = Field(ge=1)
    max_facts: int = Field(ge=1)
