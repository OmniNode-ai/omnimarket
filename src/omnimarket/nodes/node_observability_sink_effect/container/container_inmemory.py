# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the local observability sink the effect degrades to (OMN-21695).

The handler never constructs the sink itself: when the runtime injects no Kafka or
Postgres sink it takes the one this module binds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_observability_sink_effect.handlers.handler_observability_sink_effect import (
        InMemoryObservabilitySink,
    )


def bind_local_sink() -> InMemoryObservabilitySink:
    """Bind a fresh in-memory sink."""
    from omnimarket.nodes.node_observability_sink_effect.handlers.handler_observability_sink_effect import (
        InMemoryObservabilitySink,
    )

    return InMemoryObservabilitySink()
