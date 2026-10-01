# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-resident projection read effect node (OMN-20159).

Answers a typed read of one contract-declared projection exposure from the
table its writer materializes, through the runtime's own database binding.
It reads with the same function the standalone projection API's
``GET /projection/{topic}`` route uses
(:func:`omnimarket.projection.read_page.read_projection_page`), so the two
read paths return the same page for the same request.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)

__all__ = [
    "HandlerProjectionRead",
    "ModelProjectionReadRequest",
    "ModelProjectionReadResult",
]
