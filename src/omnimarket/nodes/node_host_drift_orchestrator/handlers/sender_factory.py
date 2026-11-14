# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the lab-work sender for the host drift orchestrator (OMN-24197)."""

from __future__ import annotations

from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftBackend,
    ModelHostDriftOrchestratorOverlay,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.protocols import (
    ProtocolHostDriftLabWorkSender,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_in_memory import (
    InMemoryHostDriftSender,
)


def build_sender(
    overlay: ModelHostDriftOrchestratorOverlay,
) -> ProtocolHostDriftLabWorkSender:
    """Build the sender for the overlay's backend."""
    if overlay.backend is EnumHostDriftBackend.IN_MEMORY:
        return InMemoryHostDriftSender()
    return SenderLabWorkBus(
        overlay.lab_work.bus_lane,
        overlay.lab_work.workspace_root,
        kafka_bootstrap=overlay.lab_work.kafka_bootstrap,
    )


__all__: list[str] = ["build_sender"]
