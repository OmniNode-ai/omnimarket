# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The piece records' backend interface: one transaction per request (plan section 7.5)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_order_advance import (
    ModelPieceRecordRequest,
    ModelPieceRecordResult,
)


@runtime_checkable
class ProtocolPieceRecords(Protocol):
    """Each piece's state, the newest fence token and each holder's token; a request commits whole or not."""

    def transact(self, request: ModelPieceRecordRequest) -> ModelPieceRecordResult:
        """Apply one request in one transaction and return its typed answer."""
        ...
