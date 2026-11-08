# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory profile, the binding site: every seam scripted, the store an in-memory conditional store."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import ModelFloorFillConfig
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedClock,
    ScriptedCommandForward,
    ScriptedFacts,
    ScriptedLedger,
    ScriptedPool,
    ScriptedStarter,
    ScriptedTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer


def build_inmemory_handler(config: ModelFloorFillConfig) -> HandlerFloorFillCommand:
    """A handler on scripted seams with no floor to read: every pass answers ``UNKNOWN floor-unread``."""
    orchestrator = FloorFillOrchestrator(
        config=config,
        candidates=HandlerFloorFillCandidates(),
        reducer=HandlerFloorFillReducer(),
        lines=HandlerFloorFillLines(),
        store=InMemoryFloorStateStore(),
        facts=ScriptedFacts(None),
        pool=ScriptedPool(0),
        ledger=ScriptedLedger(),
        tickets=ScriptedTickets(),
        runner=ScriptedStarter(),
        clock=ScriptedClock(datetime(1970, 1, 1, tzinfo=UTC)),
    )
    return HandlerFloorFillCommand(
        config=config,
        orchestrator=orchestrator,
        runner=ScriptedCommandForward(ModelRunOutcome(state="failed", cause="unbound")),
    )


__all__: list[str] = ["build_inmemory_handler"]
