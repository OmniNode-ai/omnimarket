# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory storage backend: simulated hosts with a free-bytes counter each."""

from __future__ import annotations

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
    ModelStorageCommand,
    ModelStorageHost,
)


class BackendHostStorageInMemory:
    def __init__(self, overlay: ModelHostStorageOverlay) -> None:
        self._free = {h.name: h.simulated_free_bytes for h in overlay.hosts}
        self.ran: list[tuple[str, str]] = []
        #: Hosts whose reading is unavailable, for the error chain.
        self.unreadable: set[str] = set()
        #: Commands that exit non-zero, for the error chain.
        self.failing: set[str] = set()

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        if host.name in self.unreadable:
            return None
        return self._free[host.name]

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun:
        self.ran.append((host.name, command.name))
        if command.name in self.failing:
            return ModelHostStorageRun(exit_code=1, output_tail="simulated failure")
        self._free[host.name] = max(
            0, self._free[host.name] + command.simulated_effect_bytes
        )
        return ModelHostStorageRun(exit_code=0)
