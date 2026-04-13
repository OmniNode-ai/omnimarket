# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_snapshot_effect — Structured PR scanning for merge sweep pipeline."""

from omnimarket.nodes.node_pr_snapshot_effect.handlers.handler_pr_snapshot import (
    HandlerPrSnapshot,
)

__all__ = [
    "HandlerPrSnapshot",
    "NodePrSnapshotEffect",
]


class NodePrSnapshotEffect(HandlerPrSnapshot):
    """ONEX entry-point wrapper for HandlerPrSnapshot."""
