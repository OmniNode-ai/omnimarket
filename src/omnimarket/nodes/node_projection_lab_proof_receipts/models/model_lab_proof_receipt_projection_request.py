# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input to the pure lab-proof-receipt fold (OMN-19566)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_lab_proof_receipts.models.model_lab_proof_receipt_event import (
    ModelLabProofReceiptEvent,
)
from omnimarket.nodes.node_projection_lab_proof_receipts.models.model_lab_proof_receipt_row import (
    ModelLabProofReceiptRow,
)


class ModelLabProofReceiptProjectionRequest(BaseModel):
    """The stored row for the event's key, if any, and the event itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelLabProofReceiptEvent
    previous_row: ModelLabProofReceiptRow | None = None


__all__ = ["ModelLabProofReceiptProjectionRequest"]
