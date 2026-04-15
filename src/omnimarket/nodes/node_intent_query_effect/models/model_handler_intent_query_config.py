# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the intent query handler.

This module defines the configuration model for HandlerIntentQuery,
providing validated settings for timeout, time ranges, and defaults.

Example::

    from omnimarket.nodes.node_intent_query_effect.models import (
        ModelHandlerIntentQueryConfig,
    )

    # Default configuration
    config = ModelHandlerIntentQueryConfig()

    # Custom configuration
    config = ModelHandlerIntentQueryConfig(
        timeout_seconds=30.0,
        default_time_range_hours=48,
        default_limit=50,
        default_min_confidence=0.5,
    )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelHandlerIntentQueryConfig"]


class ModelHandlerIntentQueryConfig(BaseModel):
    """Configuration for HandlerIntentQuery.

    Provides validated configuration settings for intent query operations
    including timeouts, default query parameters, and thresholds.

    Attributes:
        timeout_seconds: Maximum time to wait for adapter operations.
            Must be between 1.0 and 60.0 seconds.
        default_time_range_hours: Default time range for distribution and
            recent queries when not explicitly specified.
        default_limit: Default result limit for session and recent queries
            when not explicitly specified.
        default_min_confidence: Default minimum confidence threshold for
            filtering results.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )

    timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Maximum time for adapter operations in seconds",
    )
    default_time_range_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="Default time range in hours for distribution/recent queries",
    )
    default_limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Default result limit for session/recent queries",
    )
    default_min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Default minimum confidence threshold for filtering",
    )
