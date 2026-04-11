# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the mock database handler.

HandlerDbMock.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelHandlerDbMockConfig",
]


class ModelHandlerDbMockConfig(BaseModel):
    """Configuration for the mock database handler.

    Attributes:
        simulate_latency_ms: Simulated latency for operations in milliseconds.
            Set to 0 for instant responses.
        case_sensitive: Whether text search is case-sensitive. Defaults to False.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    simulate_latency_ms: int = Field(
        default=0,
        ge=0,
        description="Simulated latency in milliseconds",
    )
    case_sensitive: bool = Field(
        default=False,
        description="Whether text search is case-sensitive",
    )
