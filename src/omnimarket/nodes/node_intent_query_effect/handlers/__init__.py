# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Intent Query Effect handlers.

This package contains IHandler implementations for the intent_query_effect node.

Handlers:
    - HandlerIntentQuery: Main handler for processing intent query events

Example::

    import os
    from omnibase_core.container import ModelONEXContainer
    from omnimarket.nodes.node_intent_query_effect.handlers import HandlerIntentQuery

    # Create handler with container (handler owns adapter lifecycle)
    container = ModelONEXContainer()
    handler = HandlerIntentQuery(container)
    await handler.initialize(
        connection_uri=os.getenv("MEMGRAPH_URI", "bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}"),
        auth=(os.getenv("MEMGRAPH_USER", ""), os.getenv("MEMGRAPH_PASSWORD", "")),
    )

    # Execute query
    response = await handler.execute(request_event)

    # Shutdown releases all resources
    await handler.shutdown()

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.

.. versionchanged:: 0.2.0
    Refactored to container-driven pattern for OMN-1577.
"""

from omnimarket.nodes.node_intent_query_effect.handlers.handler_intent_query import (
    HandlerIntentQuery,
)

__all__ = ["HandlerIntentQuery"]
