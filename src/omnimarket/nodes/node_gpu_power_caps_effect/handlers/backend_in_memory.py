# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory power caps backend: simulated hosts with a card list each."""

from __future__ import annotations

from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    ModelGpuCard,
    ModelGpuSetRun,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsHost,
    ModelGpuPowerCapsOverlay,
)


class BackendGpuPowerCapsInMemory:
    def __init__(self, overlay: ModelGpuPowerCapsOverlay) -> None:
        self._cards = {host.name: list(host.simulated_cards) for host in overlay.hosts}
        self.set_calls: list[tuple[str, int, int]] = []
        #: Hosts whose reading is unavailable, for the error chain.
        self.unreadable: set[str] = set()
        #: (host, index) pairs whose write fails, for the error chain.
        self.failing_indexes: set[tuple[str, int]] = set()
        #: (host, index) pairs whose write is accepted but changes nothing.
        self.ignoring: set[tuple[str, int]] = set()

    def read_cards(
        self, host: ModelGpuPowerCapsHost
    ) -> tuple[ModelGpuCard, ...] | None:
        if host.name in self.unreadable:
            return None
        return tuple(self._cards[host.name])

    def set_limit(
        self, host: ModelGpuPowerCapsHost, index: int, watts: int
    ) -> ModelGpuSetRun:
        self.set_calls.append((host.name, index, watts))
        if (host.name, index) in self.failing_indexes:
            return ModelGpuSetRun(exit_code=1, output_tail="simulated failure")
        if (host.name, index) in self.ignoring:
            return ModelGpuSetRun(exit_code=0)
        cards = self._cards[host.name]
        for position, card in enumerate(cards):
            if card.index == index:
                cards[position] = card.model_copy(update={"power_limit_watts": watts})
                return ModelGpuSetRun(exit_code=0)
        return ModelGpuSetRun(exit_code=1, output_tail="no such card index")
