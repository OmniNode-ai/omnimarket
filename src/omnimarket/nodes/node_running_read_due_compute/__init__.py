# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_running_read_due_compute: the running-read due row at merge and its alarm (OMN-26491)."""

from omnimarket.nodes.node_running_read_due_compute.handlers.handler_running_read_due import (
    HandlerRunningReadDue,
)


class NodeRunningReadDueCompute(HandlerRunningReadDue):
    """ONEX entrypoint for the running-read due row."""


__all__ = ["HandlerRunningReadDue", "NodeRunningReadDueCompute"]
