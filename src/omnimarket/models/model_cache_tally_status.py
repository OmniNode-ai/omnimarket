# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the publish status carries about the source read cache: each step's tally of the cycle and a red row for a step that keeps reading."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCacheStepTally(BaseModel):
    """One board step's cache tally of the cycle and the cycles in a row it stored nothing and read its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str = Field(min_length=1)
    hits: int
    misses: int
    stores: int
    source_reads: int
    source_seconds: float
    no_store_cycles: int = Field(ge=0)


class ModelCacheRedRow(BaseModel):
    """A step that stored nothing and read its source for the contract's number of cycles in a row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str = Field(min_length=1)
    cycles: int = Field(ge=1)
    detail: str


class ModelCacheTallyStatus(BaseModel):
    """The ``cache_tally`` block of the publish status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[ModelCacheStepTally, ...] = ()
    red_rows: tuple[ModelCacheRedRow, ...] = ()
