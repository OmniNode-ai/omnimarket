# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Navigation History Reducer - ONEX Reducer Node (OMN-2584).

Persists completed navigation sessions to PostgreSQL and (for successful paths)
to Qdrant, enabling retrieval-augmented navigation over prior execution paths.

Node Type: REDUCER
Handler:   HandlerNavigationHistoryReducer
Writer:    HandlerNavigationHistoryWriter

Routing:
- Success → PostgreSQL (navigation_sessions) + Qdrant (navigation_paths)
- Failure → PostgreSQL only (never written to Qdrant)

Fire-and-forget pattern::

    handler = HandlerNavigationHistoryReducer()
    await handler.initialize()

    asyncio.create_task(
        handler.execute(ModelNavigationHistoryRequest(session=session))
    )

.. note::
    Local type definitions (ModelNavigationSession, ModelPlanStep, NavigationOutcome) will
    be replaced by imports from omnibase_core once OMN-2540 and OMN-2561 land.

.. versionadded:: 0.4.0
    Initial implementation for OMN-2584 Navigation History Storage.
"""

from omnimarket.nodes.node_navigation_history_reducer.handlers import (
    HandlerNavigationHistoryReducer,
    HandlerNavigationHistoryWriter,
)
from omnimarket.nodes.node_navigation_history_reducer.models import (
    ModelNavigationHistoryRequest,
    ModelNavigationHistoryResponse,
    ModelNavigationSession,
    ModelPlanStep,
    NavigationOutcome,
)
from omnimarket.nodes.node_navigation_history_reducer.node_navigation_history_reducer import (
    NodeNavigationHistoryReducer,
)

__all__ = [
    "HandlerNavigationHistoryReducer",
    "HandlerNavigationHistoryWriter",
    "ModelNavigationHistoryRequest",
    "ModelNavigationHistoryResponse",
    "ModelNavigationSession",
    "ModelPlanStep",
    "NavigationOutcome",
    "NodeNavigationHistoryReducer",
]
