# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build exception queue handlers and their dependencies."""

from __future__ import annotations

import os

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.protocols.protocol_ledger_rows_reader import ProtocolLedgerRowsReader

from ..handler import (
    HandlerExceptionCli,
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerExceptionQueueRead,
    HandlerExceptionResolve,
    HandlerOpenStops,
    HandlerStopEvent,
)
from ..handlers.handler_andon_ledger_holds import HandlerAndonLedgerHolds
from ..node import NodeExceptionQueueEffect

__all__ = [
    "create_handler_andon_ledger_holds",
    "create_handler_exception_cli",
    "create_handler_exception_emit",
    "create_handler_exception_queue",
    "create_handler_exception_queue_read",
    "create_handler_exception_resolve",
    "create_handler_open_stops",
    "create_handler_stop_event",
    "create_node_exception_queue_effect",
]


def create_handler_exception_emit() -> HandlerExceptionEmit:
    return HandlerExceptionEmit()


def create_handler_exception_queue() -> HandlerExceptionQueue:
    return HandlerExceptionQueue()


def create_handler_exception_resolve() -> HandlerExceptionResolve:
    return HandlerExceptionResolve()


def create_handler_stop_event() -> HandlerStopEvent:
    return HandlerStopEvent()


def create_handler_open_stops(
    ledger_reader: ProtocolLedgerRowsReader | None = None,
) -> HandlerOpenStops:
    """Open-stops reads HOLD rows through the ledger reader node; the default runs it under the environment."""
    return HandlerOpenStops(
        ledger_reader=ledger_reader
        if ledger_reader is not None
        else SubprocessLedgerRowsReader(os.environ)
    )


def create_handler_andon_ledger_holds(
    ledger_reader: ProtocolLedgerRowsReader | None = None,
) -> HandlerAndonLedgerHolds:
    return HandlerAndonLedgerHolds(
        ledger_reader=ledger_reader
        if ledger_reader is not None
        else SubprocessLedgerRowsReader(os.environ)
    )


def create_handler_exception_queue_read(
    queue: HandlerExceptionQueue | None = None,
) -> HandlerExceptionQueueRead:
    return HandlerExceptionQueueRead(
        queue=queue if queue is not None else create_handler_exception_queue(),
    )


def create_handler_exception_cli(
    emit: HandlerExceptionEmit | None = None,
    queue: HandlerExceptionQueue | None = None,
    resolve: HandlerExceptionResolve | None = None,
) -> HandlerExceptionCli:
    return HandlerExceptionCli(
        emit=emit if emit is not None else create_handler_exception_emit(),
        queue=queue if queue is not None else create_handler_exception_queue(),
        resolve=resolve if resolve is not None else create_handler_exception_resolve(),
    )


def create_node_exception_queue_effect() -> NodeExceptionQueueEffect:
    return NodeExceptionQueueEffect()
