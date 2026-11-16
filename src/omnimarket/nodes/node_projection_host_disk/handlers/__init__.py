# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for the host disk projection (OMN-23865).

A rule-7a pair: the pure fold, and the effect-class writer the runtime calls.
"""

from omnimarket.nodes.node_projection_host_disk.handlers.handler_host_disk_writer import (
    HostDiskProjectionWriter,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_projection_host_disk import (
    HandlerProjectionHostDisk,
)

__all__ = [
    "HandlerProjectionHostDisk",
    "HostDiskProjectionWriter",
]
