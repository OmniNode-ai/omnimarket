# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory profile, the binding site: the handlers on an in-memory ticket store."""

from __future__ import annotations

from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_inmemory_cluster_ticket_store import (
    InMemoryClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers import (
    HandlerBaselineClusterTicket,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)


def build_inmemory_handlers(
    store: ProtocolClusterTicketStore | None = None,
) -> HandlerBaselineClusterTicket:
    """The report handler on one store (a fresh in-memory one by default)."""
    bound = store if store is not None else InMemoryClusterTicketStore()
    return HandlerBaselineClusterTicket(bound)


__all__: list[str] = ["build_inmemory_handlers"]
