# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""NodeNavigationHistoryReducer - ONEX 4-Node Reducer.

Declarative node class. All business logic lives in
``HandlerNavigationHistoryReducer``; this class is purely a container-aware
entry point following ONEX node conventions.

.. versionadded:: 0.4.0
    Initial implementation for OMN-2584 Navigation History Storage.
"""

from __future__ import annotations

from omnimemory.nodes.base import BaseReducerNode, ContainerType

from omnimarket.nodes.node_navigation_history_reducer.handlers import (
    HandlerNavigationHistoryReducer,
)


class NodeNavigationHistoryReducer(BaseReducerNode):
    """ONEX Reducer node: persists completed navigation sessions.

    It receives completed ``NavigationSession`` records from the navigation
    planner and delegates all persistence logic to
    ``HandlerNavigationHistoryReducer``.

    Following ONEX patterns, this class is purely declarative:
    - No business logic here.
    - All I/O is in the handler.
    - Container injection provided by ``BaseReducerNode``.

    Usage::

        container = ModelONEXContainer(...)
        node = NodeNavigationHistoryReducer(container)
        await node.handler.initialize()

        # Fire-and-forget from navigation session:
        asyncio.create_task(
            node.handler.execute(ModelNavigationHistoryRequest(session=session))
        )

        await node.handler.shutdown()

    Attributes:
        handler: The ``HandlerNavigationHistoryReducer`` instance bound to this
            node. Configured from container settings where available.
    """

    def __init__(self, container: ContainerType) -> None:
        """Initialize the node and its handler.

        Args:
            container: ONEX container providing dependency injection and
                configuration. The handler is constructed with defaults;
                container-provided configuration support can be added as
                the platform matures.
        """
        super().__init__(container)
        self.handler = HandlerNavigationHistoryReducer()
