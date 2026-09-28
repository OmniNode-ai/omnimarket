# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed output of the pure lab-proof-receipt fold (OMN-19566)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_lab_proof_receipts.models.model_lab_proof_receipt_row import (
    ModelLabProofReceiptRow,
)


class ModelLabProofReceiptProjectionResult(BaseModel):
    """The row to store, and whether it replaces what is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLabProofReceiptRow
    applied: bool


__all__ = ["ModelLabProofReceiptProjectionResult"]
