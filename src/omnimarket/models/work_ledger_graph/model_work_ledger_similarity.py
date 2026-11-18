# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and cited results for work ledger similarity."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class EnumLedgerSimilarityOperation(StrEnum):
    INDEX_LEDGER_ROWS = "index_ledger_rows"
    SIMILAR_RULINGS = "similar_rulings"
    SIMILAR_BLOCKERS = "similar_blockers"


class ModelLedgerSimilarityTags(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str | None = None
    kind: str | None = None
    pr: str | None = None
    head: str | None = None
    outcome: str | None = None


class ModelLedgerSimilarityFilter(ModelLedgerSimilarityTags):
    ticket: str | None = None


class ModelLedgerSimilarityRow(ModelLedgerSimilarityTags):
    row_id: str = Field(min_length=1)
    row_ts: AwareDatetime
    row_type: str = Field(min_length=1)
    text: str
    question: str | None = None
    ticket: tuple[str, ...] = ()


class ModelLedgerSimilarityCursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_ts: AwareDatetime
    row_id: str = Field(min_length=1)


class ModelLedgerSimilarityProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: AwareDatetime
    until: AwareDatetime
    after: ModelLedgerSimilarityCursor | None = None
    page_size: int = Field(default=64, ge=1, le=128)

    @model_validator(mode="after")
    def check_window(self) -> "ModelLedgerSimilarityProjection":
        if self.until <= self.since:
            raise ValueError("until must be after since")
        if self.after is not None and not self.since <= self.after.row_ts < self.until:
            raise ValueError("cursor must be in the projection window")
        return self


class ModelLedgerSimilarityIndexRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str = Field(min_length=1)
    projection: ModelLedgerSimilarityProjection


class ModelLedgerSimilarityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    ledger_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    before: AwareDatetime
    tags: ModelLedgerSimilarityFilter = Field(
        default_factory=ModelLedgerSimilarityFilter
    )
    limit: int = Field(default=5, ge=1, le=50)
    method: Literal["embedding", "word_overlap"] = "embedding"


class ModelLedgerSimilarityHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLedgerSimilarityRow
    score: float
    next_terminal: ModelLedgerSimilarityRow | None = None
