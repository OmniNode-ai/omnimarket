# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrHandoffDodReceiptRequest for the handoff receipt check."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelPrHandoffDodReceiptRequest(BaseModel):
    """The ticket and immutable PR head the caller is about to hand off."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    ticket: str
    head: str
    receipts_root: str | None = Field(
        default=None,
        description="Explicit receipt-store overlay on the executing host.",
    )
