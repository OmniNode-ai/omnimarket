# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Response model for intent storage operations.

This model defines the output contract for the intent_storage_effect node.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelIntentRecordResponse", "ModelIntentStorageResponse"]


class ModelIntentRecordResponse(BaseModel):
    """Embedded intent record in query responses."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    intent_id: UUID = Field(
        ...,
        description="Unique identifier for the intent",
    )
    intent_category: str = Field(
        ...,
        description="The classified intent category",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Keywords associated with the intent",
    )
    created_at_utc: str = Field(
        ...,
        description="UTC timestamp when the intent was created (ISO format)",
    )
    correlation_id: UUID | None = Field(
        default=None,
        description="Correlation ID linking to a specific request",
    )


class ModelIntentStorageResponse(BaseModel):
    """Response model for intent storage operations.

    The response fields populated depend on the operation:
    - store: status, intent_id, created, execution_time_ms
    - get_session: status, intents, total_count, execution_time_ms
    - get_distribution: status, distribution, total_intents, execution_time_ms

    Attributes:
        status: Operation status.
        intent_id: For store operations, the created/updated intent ID.
        created: For store operations, whether a new intent was created.
        intents: For get_session operations, list of intent records.
        total_count: For queries, total number of results.
        distribution: For get_distribution, category -> count mapping.
        total_intents: For get_distribution, total intents counted.
        time_range_hours: For get_distribution, the queried time range.
        execution_time_ms: Operation execution time in milliseconds.
        error_message: Error details if status is 'error'.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    status: Literal["success", "error", "not_found", "no_results"] = Field(
        ...,
        description="Operation status",
    )
    intent_id: UUID | None = Field(
        default=None,
        description="For store operations, the created/updated intent ID",
    )
    session_id: str | None = Field(
        default=None,
        description="Session identifier",
    )
    created: bool = Field(
        default=False,
        description="For store operations, True if new intent created",
    )
    intents: list[ModelIntentRecordResponse] = Field(
        default_factory=list,
        description="For get_session operations, list of intent records",
    )
    total_count: int = Field(
        default=0,
        ge=0,
        description="Total number of results",
    )
    distribution: dict[str, int] = Field(
        default_factory=dict,
        description="For get_distribution, category -> count mapping",
    )
    total_intents: int = Field(
        default=0,
        ge=0,
        description="For get_distribution, total intents counted",
    )
    time_range_hours: int = Field(
        default=24,
        ge=1,
        description="For get_distribution, the queried time range",
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Operation execution time in milliseconds",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if status is 'error'",
    )
