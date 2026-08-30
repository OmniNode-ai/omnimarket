# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read model for the open-obligations projection (OMN-17019).

This is the shape a READER sees -- the projection_api response, not the write
row. It carries the two DERIVED columns (``state``, ``owed_by``) that the
handler never writes and that Postgres computes with
``GENERATED ALWAYS ... STORED``. Keeping the read model separate from
``ModelOpenObligationRow`` is what stops a renderer from ever constructing a
row with a hand-set ``state``: the write path has no such field, and the read
path cannot be written back.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationState,
)


class ModelOpenObligationView(BaseModel):
    """One obligation as a reader sees it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    obligation_id: str = Field(..., min_length=1)
    state: EnumObligationState = Field(
        ...,
        description="Derived: COALESCE(closed_state,'open'). Never writable.",
    )
    last_event_at: datetime = Field(
        ..., description="emitted_at of the most recently applied event."
    )
    owed_by: str | None = Field(
        default=None,
        description="Derived: COALESCE(transferred_owed_by, original_owed_by).",
    )
    asked_by: str | None = Field(default=None)
    acceptance_condition: str | None = Field(default=None)
    opened_summary: str | None = Field(default=None)
    ticket_id: str | None = Field(default=None)
    evidence_uri: str | None = Field(default=None)
    delivery_state: str | None = Field(default=None)
    superseded_by: str | None = Field(default=None)
    abandon_reason: str | None = Field(default=None)


__all__: list[str] = ["ModelOpenObligationView"]
