# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for ``node_work_ledger_emit_effect`` (OMN-19513)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.events.work.model_work_event_union import ModelWorkEvent
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.model_ledger_row_event import (
    DEFAULT_LEDGER_ID,
)


class ModelWorkLedgerEmitRequest(BaseModel):
    """One ledger row to publish as its typed event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: str = Field(
        ..., min_length=1, description="The exact ledger row, as appended."
    )
    ledger_id: str = Field(default=DEFAULT_LEDGER_ID, min_length=1)
    event: ModelWorkEvent | None = None
    provenance_kind: Literal["markdown", "typed"] = "markdown"
    ledger_seq: int | None = Field(
        default=None,
        ge=1,
        description="The sequence the judge assigned the row when it accepted it (OMN-23152).",
    )
    source: str = Field(
        default="ledger-emit",
        min_length=1,
        description=(
            "Which path wrote the row: ledger-emit (this node, the default) or onex-ledger "
            "(the dual write, which carries a ledger_seq; the projection refuses it without one)."
        ),
    )


__all__: list[str] = ["ModelWorkLedgerEmitRequest"]
