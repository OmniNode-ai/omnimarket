# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the host storage effect."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelStorageCommand,
    ModelStorageHost,
)


class ProtocolHostStorageBackend(Protocol):
    """Reads free bytes and runs declared commands. Never raises for a failed command."""

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        """Free bytes on ``path`` of the host, or None when they cannot be read."""
        ...

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun: ...
