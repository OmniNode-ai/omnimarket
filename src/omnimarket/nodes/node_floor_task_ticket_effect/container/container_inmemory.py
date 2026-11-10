# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory profile, the binding site: the handlers on an in-memory ticket store."""

from __future__ import annotations

from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketEntryCheck,
    HandlerFloorTaskTicketMint,
)


def build_inmemory_handlers(
    store: ProtocolFloorTicketStore | None = None,
) -> tuple[HandlerFloorTaskTicketMint, HandlerFloorTaskTicketEntryCheck]:
    """The mint and the entry check on one store (a fresh in-memory one by default)."""
    bound = store if store is not None else InMemoryFloorTicketStore()
    return HandlerFloorTaskTicketMint(bound), HandlerFloorTaskTicketEntryCheck(bound)


__all__: list[str] = ["build_inmemory_handlers"]
