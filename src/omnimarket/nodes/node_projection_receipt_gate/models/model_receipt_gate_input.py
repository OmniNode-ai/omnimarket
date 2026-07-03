# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed reducer input for receipt-gate projection updates."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_receipt_gate.models.model_receipt_gate_row import (
    ModelReceiptGateRow,
)


class ModelReceiptGateProjectionInput(BaseModel):
    """Envelope consumed by the receipt-gate projection reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: list[ModelReceiptGateRow] = Field(default_factory=list)
    event: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ModelReceiptGateProjectionInput"]
