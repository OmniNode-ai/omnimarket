# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate, deduplicate and append ledger rows; no bus code (OMN-20275)."""

import re
import socket
import time
from collections.abc import Callable
from typing import Literal

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerAppendRunner,
    ProtocolLedgerReader,
)

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ([^|\r\n]+) \|")
_BUS_ROW_TYPES = frozenset(
    {
        "CLAIM",
        "STATUS",
        "TERMINAL",
        "HOLD",
        "RELEASE",
        "MSG",
        "ACK",
        "FRICTION",
        "CORRECTION",
    }
)
_TYPE_REFUSAL = (
    "RULING and OPERATOR-CONSENT rows are not accepted over the bus until the "
    "receipt can name an authenticated principal (OMN-20275)"
)


class HandlerWorkLedgerAppendEffect:
    def __init__(
        self,
        runner: ProtocolLedgerAppendRunner | None = None,
        reader: ProtocolLedgerReader | None = None,
        host_name: str | None = None,
        *,
        retry_sleep_s: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner
        self._reader = reader
        # The runtime boot resolver constructs a declared handler from injectable
        # params alone; the serve command supplies the real runner and reader, and
        # a bare construction refuses to append rather than guess a ledger.
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._retry_sleep_s = retry_sleep_s
        self._sleep = sleep

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    @property
    def host_name(self) -> str:
        return self._host

    def _require_runner(self) -> ProtocolLedgerAppendRunner:
        if self._runner is None:
            raise RuntimeError("work-ledger append handler has no append runner")
        return self._runner

    def _require_reader(self) -> ProtocolLedgerReader:
        if self._reader is None:
            raise RuntimeError("work-ledger append handler has no ledger reader")
        return self._reader

    def handle(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        started = time.monotonic()

        def receipt(
            status: EnumWorkLedgerAppendStatus,
            code: int,
            message: str,
            lines: list[int] | None = None,
        ) -> ModelWorkLedgerAppendReceipt:
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=status,
                exit_code=code,
                message=message[-2000:],
                ledger_lines=lines or [],
                ledger_host=self.host_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        rows: list[tuple[str, list[str]]] = []
        for line in request.rows.splitlines():
            match = _ROW_START.match(line)
            if match:
                rows.append((match[1], [line]))
            elif rows:
                rows[-1][1].append(line)
        if not rows:
            return receipt(EnumWorkLedgerAppendStatus.REFUSED, 65, "no ledger row")
        if any(row_type not in _BUS_ROW_TYPES for row_type, _ in rows):
            return receipt(EnumWorkLedgerAppendStatus.REFUSED, 65, _TYPE_REFUSAL)
        cell = f"req={request.request_id}"
        for index, (row_type, row_lines) in enumerate(rows, start=1):
            if not _request_lines("\n".join(row_lines), cell):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    f"row {index} ({row_type}) must carry the whole pipe cell {cell}",
                )
        # Every row of one request carries the same req= cell, so the request is
        # complete only when all of them are on the ledger. A partial count is an
        # error a person must reconcile, never a silent duplicate (model review,
        # omnibase_internal tla/ledger_bus_append/REVIEW.md).
        landed = _request_lines(self._require_reader().read_text(), cell)
        if len(landed) == len(rows):
            return receipt(
                EnumWorkLedgerAppendStatus.DUPLICATE, 0, "already appended", landed
            )
        if landed:
            return receipt(
                EnumWorkLedgerAppendStatus.ERROR,
                70,
                f"partial append: {len(landed)} of {len(rows)} rows carrying {cell} are "
                "on the ledger; reconcile by hand before the request is retried",
                landed,
            )
        for attempt in range(3):
            result = self._require_runner().append(request.rows)
            if result.exit_code != 75 or attempt == 2:
                break
            self._sleep(self._retry_sleep_s)
        tail = result.stderr or result.stdout
        landed = _request_lines(self._require_reader().read_text(), cell)
        if result.exit_code == 0 or len(landed) == len(rows):
            # A non-zero exit after the rows landed (the stranded-clone signal, 78)
            # still appended them: the receipt says accepted and carries the text.
            return receipt(EnumWorkLedgerAppendStatus.ACCEPTED, 0, tail, landed)
        status = (
            EnumWorkLedgerAppendStatus.REFUSED
            if result.exit_code == 65
            else EnumWorkLedgerAppendStatus.ERROR
        )
        return receipt(status, result.exit_code, tail)


def _request_lines(text: str, cell: str) -> list[int]:
    """Match the whole pipe cell, including a last cell without a trailing pipe."""
    pattern = re.compile(r" \| " + re.escape(cell) + r"(?= \||$)")
    return [
        index
        for index, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line)
    ]
