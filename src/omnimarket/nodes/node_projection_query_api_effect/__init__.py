# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime projection query node (OMN-21288, T17 of the one-runtime plan).

Answers every route the standalone projection API service answers, from the
tables the projection writers materialize, tenant-scoped. Its contract declares
an ``http_mount`` (an ASGI factory and a path prefix) that the runtime serves on
its own HTTP port, and the routes the mounted app registers.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_query_api_effect.handlers.handler_projection_query import (
    HandlerProjectionQuery,
)
from omnimarket.nodes.node_projection_query_api_effect.models import (
    ModelProjectionQueryRequest,
    ModelProjectionQueryResponse,
)

__all__ = [
    "HandlerProjectionQuery",
    "ModelProjectionQueryRequest",
    "ModelProjectionQueryResponse",
]
