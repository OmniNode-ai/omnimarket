# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the lab-work sender for the convergence orchestrator (OMN-24198)."""

from __future__ import annotations

from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceBackend,
    ModelConvergenceOrchestratorOverlay,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.protocols import (
    ProtocolConvergenceLabWorkSender,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.sender_in_memory import (
    InMemoryConvergenceSender,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)


def build_sender(
    overlay: ModelConvergenceOrchestratorOverlay,
) -> ProtocolConvergenceLabWorkSender:
    """Build the sender for the overlay's backend."""
    if overlay.backend is EnumConvergenceBackend.IN_MEMORY:
        return InMemoryConvergenceSender()
    return SenderLabWorkBus(
        overlay.lab_work.bus_lane,
        overlay.lab_work.workspace_root,
        kafka_bootstrap=overlay.lab_work.kafka_bootstrap,
    )


__all__: list[str] = ["build_sender"]
