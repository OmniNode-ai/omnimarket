# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the host pool alarm with its exception queue dependencies."""

from __future__ import annotations

from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_resolve,
)
from omnimarket.nodes.node_exception_queue_effect.handler import (
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerExceptionResolve,
)

from ..handler import HandlerHostPoolAlarm
from ..node import NodeHostPoolAlarmEffect

__all__ = ["create_handler_host_pool_alarm", "create_node_host_pool_alarm_effect"]


def create_handler_host_pool_alarm(
    emit: HandlerExceptionEmit | None = None,
    queue: HandlerExceptionQueue | None = None,
    resolve: HandlerExceptionResolve | None = None,
) -> HandlerHostPoolAlarm:
    return HandlerHostPoolAlarm(
        emit=emit if emit is not None else create_handler_exception_emit(),
        queue=queue if queue is not None else create_handler_exception_queue(),
        resolve=resolve if resolve is not None else create_handler_exception_resolve(),
    )


def create_node_host_pool_alarm_effect() -> NodeHostPoolAlarmEffect:
    return NodeHostPoolAlarmEffect()
