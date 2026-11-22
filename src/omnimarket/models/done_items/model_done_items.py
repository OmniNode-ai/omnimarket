# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Done items folded from work-ledger events (OMN-25104).

A work item is done when a TERMINAL ledger row with an outcome of done names it. A
merged change is not done: a ticket that only has STATUS rows is never an item. A
ticket done twice keeps its newest done event, by row timestamp and then by row id.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.model_ledger_row_event import ModelLedgerRowEvent


class ModelDoneItemsRequest(BaseModel):
    """The ledger events to fold, in any order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelLedgerRowEvent, ...] = ()


class ModelDoneItem(BaseModel):
    """One done work item and the ledger row that made it done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(..., min_length=1)
    done_at: str = Field(..., description="The TERMINAL row's timestamp.")
    done_row_id: str = Field(..., description="sha256 of the TERMINAL ledger row.")
    lane: str | None = None
    pr: str | None = None
    closes: str | None = None


class ModelDoneItemsProjection(BaseModel):
    """Every done item, ordered by ticket, with their count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelDoneItem, ...] = ()
    done: int = Field(0, ge=0)
    as_of: str | None = Field(
        default=None, description="The newest event timestamp folded, if any."
    )


__all__: list[str] = [
    "ModelDoneItem",
    "ModelDoneItemsProjection",
    "ModelDoneItemsRequest",
]
