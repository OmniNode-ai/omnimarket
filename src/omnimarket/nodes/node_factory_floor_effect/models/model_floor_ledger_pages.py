# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's paged ledger reads: the settings of the step that pages them, and the result it returns (OMN-23669).

The read node refuses any answer over its byte bound, so the floor reads the window a page at a time with the node's
own ``next_cursor``. Every number the step needs (the page size, the page count, the page timeout, the time budget, the
worker count, the reads themselves) is the settings'; a read that does not reach its last page is unread and refuses the
step, never a floor drawn from the pages that did arrive.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow


class ModelFloorLedgerRead(BaseModel):
    """One declared paged read: the name the counts and refusals carry, the kinds it asks for, the term that narrows
    them, and its own window in hours (``None`` takes the caller's ``window_days`` window)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kinds: tuple[str, ...] = Field(min_length=1)
    term: str | None = Field(default=None, min_length=1)
    window_hours: int | None = Field(default=None, gt=0)


class ModelFloorLedgerPages(BaseModel):
    """What the step reads and how it pages it; every value is declared, none is in code (OMN-23669).

    The reads run together, one in its own worker. A page that does not answer, a cursor still open after
    ``max_pages`` pages, or the spent ``time_budget_seconds`` is an unread read: the step raises naming the read,
    and the previous floor stays.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_command_env: str = Field(min_length=1)
    page_size: int = Field(gt=0)
    max_pages: int = Field(gt=0)
    page_timeout_seconds: int = Field(gt=0)
    time_budget_seconds: int = Field(gt=0)
    workers: int = Field(gt=0)
    reads: tuple[ModelFloorLedgerRead, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_read_names(self) -> ModelFloorLedgerPages:
        names = [read.name for read in self.reads]
        if len(names) != len(set(names)):
            dupes = ", ".join(sorted({name for name in names if names.count(name) > 1}))
            raise ValueError(f"duplicate read names: {dupes}")
        return self


class ModelFloorLedgerReadCount(BaseModel):
    """What one read read: the pages it took and the lines it returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    pages: int = Field(ge=0)
    lines: int = Field(ge=0)


class ModelFloorLedgerPagesResult(BaseModel):
    """The floor's paged reads: the rows parsed from every read's lines, the per-read counts, and the pages they took together."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelFloorLedgerRow, ...]
    reads: tuple[ModelFloorLedgerReadCount, ...]
    pages: int = Field(ge=0)
