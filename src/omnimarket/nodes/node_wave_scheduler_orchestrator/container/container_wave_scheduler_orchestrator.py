# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_wave_scheduler_orchestrator."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_dispatch_state_store import (
    HandlerWaveDispatchStateStore,
)


def create_handler_wave_dispatch_state_store(
    *, state_dir: Path, run_id: str
) -> HandlerWaveDispatchStateStore:
    """Bind the wave dispatcher to its state directory and run."""
    return HandlerWaveDispatchStateStore(state_dir=state_dir, run_id=run_id)


__all__ = ["create_handler_wave_dispatch_state_store"]
