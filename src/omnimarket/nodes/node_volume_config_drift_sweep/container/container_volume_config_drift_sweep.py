# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_volume_config_drift_sweep."""

from __future__ import annotations

from omnimarket.nodes.node_volume_config_drift_sweep.handlers.handler_volume_config_drift_sweep import (
    NodeVolumeConfigDriftSweep,
)


def create_node_volume_config_drift_sweep() -> NodeVolumeConfigDriftSweep:
    """Bind the volume config drift sweep handler."""
    return NodeVolumeConfigDriftSweep()


__all__ = ["create_node_volume_config_drift_sweep"]
