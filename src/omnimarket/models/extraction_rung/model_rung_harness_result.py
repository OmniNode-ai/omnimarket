# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the extraction rung harness (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.extraction_rung.model_chunk_derivation import (
    ModelChunkDerivation,
)
from omnimarket.models.extraction_rung.model_rung_call_verdict import (
    ModelRungCallVerdict,
)


class ModelRungHarnessResult(BaseModel):
    """parse_failures and misses are two counts and are never summed into one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str = Field(min_length=1)
    verdicts: tuple[ModelRungCallVerdict, ...]
    calls: int = Field(ge=0)
    hits: int = Field(ge=0)
    parse_failures: int = Field(ge=0)
    misses: int = Field(ge=0)
    corrupt_recordings: int = Field(ge=0)
    attribution_mismatches: int = Field(ge=0)
    chunk: ModelChunkDerivation
