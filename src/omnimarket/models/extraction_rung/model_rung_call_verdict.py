# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness verdict on one replayed rung call (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.extraction_rung.enum_rung_call_outcome import (
    EnumRungCallOutcome,
)


class ModelRungCallVerdict(BaseModel):
    """replayed_b64 is the recorded bytes, replayed from the recording alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1)
    outcome: EnumRungCallOutcome
    attribution_ok: bool
    replayed_b64: str
    replayed_digest: str
    proposals: tuple[str, ...]
    parse_failures: int = Field(ge=0)
    misses: int = Field(ge=0)
