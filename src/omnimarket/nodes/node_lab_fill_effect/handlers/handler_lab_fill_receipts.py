# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipts: read each detached lane's runner receipt for its placement (OMN-20668).

The loop is code, not an agent's patience: it re-reads every ``poll_s`` seconds
while a receipt is missing or names no host (a Codex lane until its receipt is
final or ``codex_wait_s`` has passed, because the runner's Codex sandbox probe runs
after the receipt already names the host), and stops at the window or at the
absolute end, whichever is first, so a late start reads once.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..models import ModelLabFillReceiptsRequest, ModelLabFillReceiptsResult
from ..protocols import (
    LabFillPortError,
    ProtocolLabFillClock,
    ProtocolLabFillReceiptReader,
)
from .helpers_lab_fill_effect import default_clock

_FIELDS = ("status", "host", "final", "exit_code", "engine")


class HandlerLabFillReceipts:
    """Read the receipts of the lanes that detached."""

    def __init__(
        self,
        reader: ProtocolLabFillReceiptReader | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import LocalReceiptReader

        self._reader = reader if reader is not None else LocalReceiptReader()
        self._clock = default_clock(clock)

    def handle(
        self, request: ModelLabFillReceiptsRequest
    ) -> ModelLabFillReceiptsResult:
        start = self._clock.now_epoch_s()
        stop = start + request.window_s
        if request.end_epoch_s:
            stop = min(stop, float(request.end_epoch_s))
        while True:
            receipts = [self._read(lane.lane, lane.receipt) for lane in request.lanes]
            now = self._clock.now_epoch_s()
            waiting = [
                r
                for lane, r in zip(request.lanes, receipts, strict=True)
                if not r["found"]
                or not r.get("host")
                or (
                    lane.engine == "codex"
                    and r.get("final") is not True
                    and now - start < request.codex_wait_s
                )
            ]
            left = stop - now
            if not waiting or left <= 0:
                break
            self._clock.sleep(min(float(request.poll_s), left))
        return ModelLabFillReceiptsResult(receipts=tuple(receipts))

    def _read(self, lane: str, path: str) -> dict[str, object]:
        try:
            receipt: Mapping[str, object] | None = self._reader.read(path)
        except LabFillPortError:
            receipt = None
        if receipt is None:
            return {"lane": lane, "found": False}
        fields = {k: receipt.get(k) for k in _FIELDS}
        return {
            "lane": lane,
            "found": True,
            **{k: v for k, v in fields.items() if v is not None},
        }
