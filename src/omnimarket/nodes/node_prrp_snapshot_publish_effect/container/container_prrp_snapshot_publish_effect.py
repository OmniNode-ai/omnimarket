# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_prrp_snapshot_publish_effect.handlers.handler_prrp_snapshot_publish import (
    HandlerPrrpSnapshotPublish,
)


def build_handler_prrp_snapshot_publish() -> HandlerPrrpSnapshotPublish:
    """Build the handler with its constructor defaults."""
    return HandlerPrrpSnapshotPublish()
