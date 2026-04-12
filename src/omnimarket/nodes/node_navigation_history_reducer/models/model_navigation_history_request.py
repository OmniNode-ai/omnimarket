# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Request model for the navigation_history_reducer node.

.. versionadded:: 0.4.0
    Initial implementation for OMN-2584 Navigation History Storage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session import (
    ModelNavigationSession,
)


class ModelNavigationHistoryRequest(BaseModel):
    """Request to record a navigation session into persistent storage.

    Attributes:
        session: The completed navigation session to persist. Success sessions
            are written to both PostgreSQL and Qdrant. Failure sessions are
            written to PostgreSQL only.
    """

    session: ModelNavigationSession = Field(
        description="Completed navigation session to record"
    )

    model_config = ConfigDict(frozen=True)
