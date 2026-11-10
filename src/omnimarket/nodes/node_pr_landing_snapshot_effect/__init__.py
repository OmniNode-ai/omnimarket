# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing snapshot step node: one tick's facts filled from the declared sources and recorded on the bus."""

from pathlib import Path

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)


class NodePrLandingSnapshotEffect(HandlerPrLandingSnapshot):
    """ONEX entry-point wrapper for HandlerPrLandingSnapshot."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerPrLandingSnapshot", "NodePrLandingSnapshotEffect"]
