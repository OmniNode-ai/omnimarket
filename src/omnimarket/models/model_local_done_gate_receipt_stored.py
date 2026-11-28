# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLocalDoneGateReceiptStored: whether a verdict's receipt reached the store."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLocalDoneGateReceiptStored(BaseModel):
    """Terminal result of storing a verdict's local Done gate receipt (OMN-26729)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(...)
    stored: bool = Field(
        ..., description="True when a receipt was appended to the store."
    )
