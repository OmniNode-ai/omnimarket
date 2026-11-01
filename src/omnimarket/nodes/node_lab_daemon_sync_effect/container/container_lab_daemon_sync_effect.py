# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_lab_daemon_sync_effect.handlers.handler_lab_daemon_sync import (
    HandlerLabDaemonSync,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.protocols import (
    ProtocolLabDaemonEventPublisher,
    ProtocolLabDaemonHost,
)


def build_handler_lab_daemon_sync(
    host: ProtocolLabDaemonHost,
    publisher: ProtocolLabDaemonEventPublisher,
    refused_topic: str,
    completed_topic: str,
) -> HandlerLabDaemonSync:
    """Build the sync handler with its host, publisher, and declared topics."""
    return HandlerLabDaemonSync(
        host=host,
        publisher=publisher,
        refused_topic=refused_topic,
        completed_topic=completed_topic,
    )
