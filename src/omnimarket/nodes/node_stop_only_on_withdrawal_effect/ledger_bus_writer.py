# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S8's ledger writer uses the existing attributed bus append caller."""

from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.model_stop_only_on_withdrawal import (
    ModelS8LedgerWrite,
    ModelS8Request,
)
from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller


class S8LedgerBusWriter:
    def __init__(
        self,
        caller: WorkLedgerAppendCaller,
        *,
        host: str,
        lane: str,
        timeout_s: float,
    ) -> None:
        self._caller = caller
        self._host = host
        self._lane = lane
        self._timeout_s = timeout_s

    async def append(
        self, write: ModelS8LedgerWrite, tick: ModelS8Request
    ) -> ModelWorkLedgerAppendReceipt:
        request_id = uuid5(
            NAMESPACE_URL, f"{tick.ledger_id}:{write.row_id}:{write.row}"
        )
        return await self._caller.append(
            ModelWorkLedgerAppendRequest(
                request_id=request_id,
                rows=f"{write.row} | req={request_id}",
                ledger_id=tick.ledger_id,
                requested_by_lane=self._lane,
                requesting_host=self._host,
                requested_at=tick.tick_at,
                append_if_absent_row_id=write.row_id,
            ),
            timeout_s=self._timeout_s,
        )
