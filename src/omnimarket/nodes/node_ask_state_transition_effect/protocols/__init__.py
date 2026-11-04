# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected canonical ledger append boundary."""

from typing import Protocol

from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)


class ProtocolAskTransitionAppender(Protocol):
    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt: ...
