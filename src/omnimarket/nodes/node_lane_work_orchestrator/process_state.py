# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one orchestrator a runtime process holds (OMN-26721).

The runtime builds each handler of the contract on its own, so the fold handler and the
intake handler reach the same workflows through this process-wide instance, as the PR
handoff orchestrator's row store is process-wide. A test installs its own with
``install_orchestrator`` and clears it with ``reset_orchestrator``.
"""

from __future__ import annotations

from omnimarket.nodes.node_lane_work_orchestrator.orchestration.lane_work_orchestrator import (
    LaneWorkOrchestrator,
)

_ORCHESTRATOR: list[LaneWorkOrchestrator] = []


def process_orchestrator() -> LaneWorkOrchestrator:
    """The process's orchestrator, built on first use."""
    if not _ORCHESTRATOR:
        _ORCHESTRATOR.append(LaneWorkOrchestrator())
    return _ORCHESTRATOR[0]


def install_orchestrator(orchestrator: LaneWorkOrchestrator) -> None:
    _ORCHESTRATOR.clear()
    _ORCHESTRATOR.append(orchestrator)


def reset_orchestrator() -> None:
    _ORCHESTRATOR.clear()


__all__: list[str] = [
    "install_orchestrator",
    "process_orchestrator",
    "reset_orchestrator",
]
