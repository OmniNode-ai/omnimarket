# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt carrier seam of node_lab_receipt_check_effect (OMN-21826)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnibase_core.models.lab_receipt.model_lab_receipt_record import (
    ModelLabReceiptRecord,
)


@runtime_checkable
class ProtocolLabReceiptCarrier(Protocol):
    """Where a lab receipt lives. The validator takes records and does not decide the carrier.

    The recommended carrier is the PR-head lab-pass receipt and its check run; until it carries the
    install fields, the lane's Lab line and ledger STATUS cells are the interim carrier.
    """

    async def read_receipts(
        self, repo: str, pr_number: int
    ) -> tuple[ModelLabReceiptRecord, ...]:
        """Every receipt record found for the pull request, whatever head each names.

        Raises when the carrier cannot be read; an empty tuple means it was read and holds none.
        """
        ...
