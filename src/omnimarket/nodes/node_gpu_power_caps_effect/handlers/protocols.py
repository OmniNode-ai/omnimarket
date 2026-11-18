# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the GPU power caps effect."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    ModelGpuCard,
    ModelGpuSetRun,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsHost,
)


class ProtocolGpuPowerCapsBackend(Protocol):
    """Reads the host's cards and applies one power limit. Never raises for a failed command."""

    def read_cards(
        self, host: ModelGpuPowerCapsHost
    ) -> tuple[ModelGpuCard, ...] | None:
        """The host's cards, or None when they cannot be read."""
        ...

    def set_limit(
        self, host: ModelGpuPowerCapsHost, index: int, watts: int
    ) -> ModelGpuSetRun: ...


class ProtocolLabWorkSender(Protocol):
    """Sends one lab work unit to a pool host and waits for its receipt. Never raises for an
    unanswered unit: it returns an infra_error receipt."""

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt: ...
