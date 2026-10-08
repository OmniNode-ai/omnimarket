# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrHandoffLedgerEffect: the handoff rows through the ledger's bus append (OMN-20636).

The rows come from node_pr_handoff_decision_compute, byte for byte what
pr-handoff wrote by hand. This effect only stamps them the way onex-ledger's
bus write does (``req=<ledger request id>`` and ``via=bus:<host>`` before each
row's free text), sends them as one ``ModelWorkLedgerAppendRequest`` to the
ledger host's ``onex work-ledger serve`` (node_work_ledger_append_effect), and
answers the orchestrator for the handoff key with exactly one
``ModelPrHandoffLedgerAppended``, whatever happened.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.models.pr_handoff import (
    EnumPrHandoffLedgerStatus,
    ModelPrHandoffLedgerAppendCommand,
    ModelPrHandoffLedgerAppended,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.protocols import (
    ProtocolPrHandoffLedgerAppender,
)

logger = logging.getLogger(__name__)

LEDGER_BUS_LANE_ENV = "ONEX_PR_HANDOFF_LEDGER_BUS_LANE"
WORKSPACE_ROOT_ENV = "OMNIBASE_PATH"
DEFAULT_TIMEOUT_S = 60.0
_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")
_STATUS: dict[EnumWorkLedgerAppendStatus, EnumPrHandoffLedgerStatus] = {
    EnumWorkLedgerAppendStatus.ACCEPTED: EnumPrHandoffLedgerStatus.ACCEPTED,
    EnumWorkLedgerAppendStatus.DUPLICATE: EnumPrHandoffLedgerStatus.DUPLICATE,
    EnumWorkLedgerAppendStatus.REFUSED: EnumPrHandoffLedgerStatus.REFUSED,
    EnumWorkLedgerAppendStatus.ERROR: EnumPrHandoffLedgerStatus.ERROR,
}


def stamp_rows(rows: str, request_id: str, host: str) -> str:
    """Every row gains ``req=`` and ``via=bus:`` before its free text (onex-ledger bus_write's rule)."""
    out: list[str] = []
    for line in rows.splitlines():
        if _ROW_START.match(line):
            parts = line.split(" | ")
            cells = [f"req={request_id}", f"via=bus:{host}"]
            if len(parts) > 2 and not _FIELD.match(parts[-1].strip()):
                line = " | ".join([*parts[:-1], *cells, parts[-1]])
            else:
                line = " | ".join([*parts, *cells])
        out.append(line)
    text = "\n".join(out)
    return text + ("\n" if rows.endswith("\n") else "")


class BusLaneLedgerAppender:
    """The production port: `onex work-ledger append`'s caller over a declared bus lane."""

    def __init__(self, lane: str, workspace_root: Path) -> None:
        self._lane = lane
        self._root = workspace_root

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
        from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

        async with open_lab_run_bus(
            bus="kafka", lane=self._lane, kafka_bootstrap=None, omni_home=self._root
        ) as bus:
            caller = WorkLedgerAppendCaller(bus)
            try:
                return await caller.append(request, timeout_s=timeout_s)
            except TimeoutError:
                return None
            finally:
                await caller.stop()


def appender_from_environment() -> ProtocolPrHandoffLedgerAppender | None:
    """The bus appender the deployment declares, or None when it declares none."""
    lane = os.environ.get(LEDGER_BUS_LANE_ENV, "").strip()
    root = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if not lane or not root:
        return None
    return BusLaneLedgerAppender(lane, Path(root))


class HandlerPrHandoffLedgerEffect:
    """One ``ModelPrHandoffLedgerAppended`` per command, accepted to pending."""

    def __init__(
        self,
        appender: ProtocolPrHandoffLedgerAppender | None = None,
        host_name: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # The runtime constructs the handler bare; the deployment's lane wires the
        # appender. With none, every command is answered error, never silence.
        self._appender = (
            appender if appender is not None else appender_from_environment()
        )
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout_s = timeout_s

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _answer(
        self,
        command: ModelPrHandoffLedgerAppendCommand,
        status: EnumPrHandoffLedgerStatus,
        message: str,
        receipt: ModelWorkLedgerAppendReceipt | None = None,
    ) -> ModelPrHandoffLedgerAppended:
        return ModelPrHandoffLedgerAppended(
            correlation_id=command.correlation_id,
            handoff_key=command.handoff_key,
            ledger_request_id=command.ledger_request_id,
            attempt=command.attempt,
            status=status,
            ledger_lines=tuple(receipt.ledger_lines) if receipt is not None else (),
            ledger_host=receipt.ledger_host if receipt is not None else "",
            message=message[-2000:],
            answered_at=self._clock(),
        )

    async def handle(
        self, command: ModelPrHandoffLedgerAppendCommand
    ) -> ModelPrHandoffLedgerAppended:
        if self._appender is None:
            return self._answer(
                command,
                EnumPrHandoffLedgerStatus.ERROR,
                f"no ledger appender is wired: set {LEDGER_BUS_LANE_ENV} and {WORKSPACE_ROOT_ENV}",
            )
        request = ModelWorkLedgerAppendRequest(
            request_id=command.ledger_request_id,
            rows=stamp_rows(command.rows, str(command.ledger_request_id), self._host),
            requested_by_lane=command.requested_by_lane,
            requesting_host=self._host,
            requested_at=command.requested_at,
        )
        try:
            receipt = await self._appender.append(request, timeout_s=self._timeout_s)
        except (OSError, RuntimeError, ValueError) as exc:
            # Every failure is answered, typed, on the bus: a bus that cannot be
            # opened (LabRunBusError is a RuntimeError), a refused connection, an
            # unreadable receipt.
            logger.warning(
                "pr-handoff ledger append %s failed: %s", command.handoff_key, exc
            )
            return self._answer(
                command, EnumPrHandoffLedgerStatus.ERROR, f"{type(exc).__name__}: {exc}"
            )
        if receipt is None:
            return self._answer(
                command,
                EnumPrHandoffLedgerStatus.PENDING,
                f"no receipt within {self._timeout_s:.0f}s; resend under the same request id",
            )
        if receipt.request_id != command.ledger_request_id:
            return self._answer(
                command,
                EnumPrHandoffLedgerStatus.ERROR,
                f"the ledger answered for request {receipt.request_id}",
                receipt,
            )
        return self._answer(command, _STATUS[receipt.status], receipt.message, receipt)


__all__: list[str] = [
    "LEDGER_BUS_LANE_ENV",
    "BusLaneLedgerAppender",
    "HandlerPrHandoffLedgerEffect",
    "appender_from_environment",
    "stamp_rows",
]
