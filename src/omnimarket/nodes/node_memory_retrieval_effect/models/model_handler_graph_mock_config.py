# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the mock graph handler.

HandlerGraphMock.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelHandlerGraphMockConfig",
]


class ModelHandlerGraphMockConfig(BaseModel):
    """Configuration for the mock graph handler.

    Attributes:
        simulate_latency_ms: Simulated latency for operations in milliseconds.
            Set to 0 for instant responses.
        max_traversal_depth: Maximum allowed traversal depth. Defaults to 10.
        bidirectional: Whether relationships are traversed bidirectionally.
            Defaults to True.
    """

    model_config = ConfigDict(frozen=True)

    simulate_latency_ms: int = Field(
        default=0,
        ge=0,
        description="Simulated latency in milliseconds",
    )
    max_traversal_depth: int = Field(
        default=10,
        ge=1,
        description="Maximum allowed traversal depth",
    )
    bidirectional: bool = Field(
        default=True,
        description="Whether to traverse relationships bidirectionally",
    )
