# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_schedule_run_reducer: holds the last run and the counts of each scheduled job (OMN-19998)."""

from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

__all__ = ["HandlerScheduleRunReducer", "NodeScheduleRunReducer"]


class NodeScheduleRunReducer(HandlerScheduleRunReducer):
    """ONEX entry-point wrapper for HandlerScheduleRunReducer."""
