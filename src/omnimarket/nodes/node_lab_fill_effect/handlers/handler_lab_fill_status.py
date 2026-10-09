# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Status: write the run's one STATUS row, and the idle alarm's FRICTION row first (OMN-20668).

The cells arrive composed by the plan node and are not changed here. Rows go to the
ledger host through the bus append under a request id derived from the run key, so a
resend answers duplicate and never appends twice. The fire's result file is written
between the two rows, as the workflow did; a friction row that was refused stops the
run before STATUS, because a failed alarm must not pass the next fire's precheck.
"""

from __future__ import annotations

import re
import socket
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

from ..models import ModelLabFillStatusWriteRequest, ModelLabFillStatusWriteResult
from ..protocols import (
    LabFillPortError,
    ProtocolLabFillClock,
    ProtocolLabFillResultWriter,
    ProtocolLabFillStatusAppender,
)
from .helpers_lab_fill_effect import default_clock, iso_utc

DEFAULT_TIMEOUT_S = 60.0
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")
_ACCEPTED = (EnumWorkLedgerAppendStatus.ACCEPTED, EnumWorkLedgerAppendStatus.DUPLICATE)


def request_id(kind: str, run_key: str, attempt: int = 1) -> UUID:
    """The same fire's same row always has the same id."""
    return uuid5(NAMESPACE_URL, f"onex:lab-fill:{kind}:{run_key}:{attempt}")


def stamp_row(row: str, req: UUID, host: str) -> str:
    """``req=`` and ``via=bus:`` before the row's free text, or at its end when it has none."""
    parts = row.split(" | ")
    cells = [f"req={req}", f"via=bus:{host}"]
    if len(parts) > 2 and not _FIELD.match(parts[-1].strip()):
        return " | ".join([*parts[:-1], *cells, parts[-1]])
    return " | ".join([*parts, *cells])


class HandlerLabFillStatus:
    """Append the run's rows and write its result file."""

    def __init__(
        self,
        appender: ProtocolLabFillStatusAppender | None = None,
        writer: ProtocolLabFillResultWriter | None = None,
        clock: ProtocolLabFillClock | None = None,
        host_name: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalResultWriter,
            appender_from_environment,
        )

        self._appender = (
            appender if appender is not None else appender_from_environment()
        )
        self._writer = writer if writer is not None else LocalResultWriter()
        self._clock = default_clock(clock)
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._timeout_s = timeout_s

    async def handle(
        self, request: ModelLabFillStatusWriteRequest
    ) -> ModelLabFillStatusWriteResult:
        if self._appender is None:
            return ModelLabFillStatusWriteResult(
                outcome="error",
                message="no ledger appender is wired: set ONEX_LAB_FILL_LEDGER_BUS_LANE and OMNIBASE_PATH",
            )
        friction_recorded = False
        if request.friction_cells:
            _, friction = await self._append(
                request, "friction", request.friction_cells
            )
            friction_recorded = isinstance(friction, ModelWorkLedgerAppendReceipt) and (
                friction.status in _ACCEPTED
            )
        result_written = self._write_result(request, friction_recorded)
        if request.friction_cells and not friction_recorded:
            return ModelLabFillStatusWriteResult(
                outcome="friction-refused",
                result_written=result_written,
                message="the idle alarm's friction row was not recorded; STATUS not appended",
            )
        row, status = await self._append(request, "status", " | ".join(request.cells))
        if status is None:
            return ModelLabFillStatusWriteResult(
                outcome="error",
                row=row,
                friction_recorded=friction_recorded,
                result_written=result_written,
                message=f"no receipt within {self._timeout_s:.0f}s; resend under the same request id",
            )
        if isinstance(status, str):
            return ModelLabFillStatusWriteResult(
                outcome="error",
                row=row,
                friction_recorded=friction_recorded,
                result_written=result_written,
                message=status[:2000],
            )
        outcomes: dict[
            EnumWorkLedgerAppendStatus,
            Literal["appended", "duplicate", "refused", "error"],
        ] = {
            EnumWorkLedgerAppendStatus.ACCEPTED: "appended",
            EnumWorkLedgerAppendStatus.DUPLICATE: "duplicate",
            EnumWorkLedgerAppendStatus.REFUSED: "refused",
            EnumWorkLedgerAppendStatus.ERROR: "error",
        }
        outcome = outcomes[status.status]
        return ModelLabFillStatusWriteResult(
            outcome=outcome,
            row=row,
            friction_recorded=friction_recorded,
            result_written=result_written,
            ledger_lines=tuple(status.ledger_lines),
            message=status.message,
        )

    async def _append(
        self, request: ModelLabFillStatusWriteRequest, kind: str, cells: str
    ) -> tuple[str, ModelWorkLedgerAppendReceipt | str | None]:
        """The row as sent, and the receipt, None for no receipt in time, or the failure text."""
        assert self._appender is not None
        req = request_id(kind, request.run_key)
        plain = f"{iso_utc(self._clock.now_epoch_s())} | {cells}"
        row = stamp_row(plain, req, self._host)
        message = ModelWorkLedgerAppendRequest(
            request_id=req,
            ledger_id=request.ledger_id,
            rows=row + "\n",
            requested_by_lane=request.requested_by_lane,
            requesting_host=self._host,
            requested_at=datetime.fromtimestamp(self._clock.now_epoch_s(), UTC),
        )
        try:
            return plain, await self._appender.append(
                message, timeout_s=self._timeout_s
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return plain, f"{type(exc).__name__}: {exc}"

    def _write_result(
        self, request: ModelLabFillStatusWriteRequest, friction_recorded: bool
    ) -> bool:
        if not request.result_path:
            return False
        value = {**request.idle, "friction_recorded": friction_recorded}
        try:
            self._writer.write(request.result_path, value)
        except (LabFillPortError, OSError):
            return False
        return True
