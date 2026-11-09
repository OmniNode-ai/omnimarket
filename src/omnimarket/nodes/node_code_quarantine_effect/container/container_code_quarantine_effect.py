# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binds the quarantine handler to the seams its overlay names: in-memory or the real files and git backend."""

from __future__ import annotations

from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_memory import (
    InMemoryGates,
    InMemoryReporter,
    InMemoryRepository,
    InMemoryStore,
    SystemClock,
)
from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_real import (
    AppendReporter,
    CommandGates,
    GitRepository,
    JsonFileStore,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumQuarantineBackend,
    ModelQuarantineOverlay,
)


def create_handler_code_quarantine(
    overlay: ModelQuarantineOverlay,
) -> HandlerCodeQuarantine:
    if overlay.backend is EnumQuarantineBackend.IN_MEMORY:
        assert overlay.in_memory is not None
        return HandlerCodeQuarantine(
            policy=overlay.policy,
            store=InMemoryStore(),
            repository=InMemoryRepository.from_overlay(overlay.in_memory),
            gates=InMemoryGates.from_overlay(overlay.in_memory),
            clock=SystemClock(),
            reporter=InMemoryReporter(),
        )
    assert overlay.files is not None
    return HandlerCodeQuarantine(
        policy=overlay.policy,
        store=JsonFileStore(overlay.files),
        repository=GitRepository(overlay.files),
        gates=CommandGates(overlay.files),
        clock=SystemClock(),
        reporter=AppendReporter(overlay.report),
    )


__all__ = ["create_handler_code_quarantine"]
