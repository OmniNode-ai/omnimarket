# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable tick collaborators; the dispatcher owns the atomic task transaction."""

from typing import Protocol

from omnimarket.models.model_stop_only_on_withdrawal import (
    ModelS8LedgerWrite,
    ModelS8Repair,
    ModelS8RepairAck,
    ModelS8Request,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendReceipt


class ProtocolS8LedgerWriter(Protocol):
    async def append(
        self, write: ModelS8LedgerWrite, tick: ModelS8Request
    ) -> ModelWorkLedgerAppendReceipt: ...


class ProtocolS8TaskDispatcher(Protocol):
    async def reopen_and_redispatch(self, repair: ModelS8Repair) -> ModelS8RepairAck:
        """Atomically reopen and dispatch once by action_id, if the revision is current."""
        ...
