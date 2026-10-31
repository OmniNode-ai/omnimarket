# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build exception queue handlers and their dependencies."""

from __future__ import annotations

from ..handler import (
    HandlerExceptionCli,
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerExceptionQueueRead,
    HandlerExceptionResolve,
)
from ..node import NodeExceptionQueueEffect

__all__ = [
    "create_handler_exception_cli",
    "create_handler_exception_emit",
    "create_handler_exception_queue",
    "create_handler_exception_queue_read",
    "create_handler_exception_resolve",
    "create_node_exception_queue_effect",
]


def create_handler_exception_emit() -> HandlerExceptionEmit:
    return HandlerExceptionEmit()


def create_handler_exception_queue() -> HandlerExceptionQueue:
    return HandlerExceptionQueue()


def create_handler_exception_resolve() -> HandlerExceptionResolve:
    return HandlerExceptionResolve()


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
