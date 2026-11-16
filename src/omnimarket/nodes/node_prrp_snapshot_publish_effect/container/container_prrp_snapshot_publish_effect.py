# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_prrp_snapshot_publish_effect.handlers.handler_prrp_snapshot_publish import (
    HandlerPrrpSnapshotPublish,
)


def build_handler_prrp_snapshot_publish() -> HandlerPrrpSnapshotPublish:
    """Build the handler on the real lab host table (the lab_hosts overlay projection).

    The handler's own default reads no table; this builder is the real-backend overlay of it."""
    return HandlerPrrpSnapshotPublish(read_lab_table=resolve_lab_hosts_table)
