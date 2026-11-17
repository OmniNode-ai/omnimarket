# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the lab-work sender for the host hygiene orchestrator (OMN-23707)."""

from __future__ import annotations

from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneBackend,
    ModelHostHygieneOrchestratorOverlay,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)


def build_sender(
    overlay: ModelHostHygieneOrchestratorOverlay,
) -> ProtocolHostHygieneLabWorkSender:
    """Build the sender for the overlay's backend."""
    if overlay.backend is EnumHostHygieneBackend.IN_MEMORY:
        return InMemoryHostHygieneSender()
    # Reuse the lab-work bus sender from node_gpu_power_caps_effect
    return SenderLabWorkBus(
        overlay.lab_work.bus_lane,
        overlay.lab_work.workspace_root,
        kafka_bootstrap=overlay.lab_work.kafka_bootstrap,
    )


__all__: list[str] = ["build_sender"]
