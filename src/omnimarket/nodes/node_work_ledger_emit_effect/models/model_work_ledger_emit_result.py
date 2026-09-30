# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for ``node_work_ledger_emit_effect`` (OMN-19513)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerEmitResult(BaseModel):
    """Outcome of emitting one row: typed event handed to the durable spool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool = Field(
        ...,
        description="True when the event is durable: on the spool, published or both.",
    )
    row_id: str | None = None
    row_type: str | None = None
    event_type: str | None = None
    topic: str | None = None
    published: bool = Field(
        default=False,
        description="False when the row only reached the spool (a lab outage).",
    )
    refusal: str | None = Field(
        default=None,
        description="Set when the row cannot be typed; nothing was spooled.",
    )
    error: str | None = Field(
        default=None,
        description="Set when typing succeeded but the spool refused the event.",
    )


__all__: list[str] = ["ModelWorkLedgerEmitResult"]
