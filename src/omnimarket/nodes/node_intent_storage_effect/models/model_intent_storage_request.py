# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Request model for intent storage operations.

This model defines the input contract for the intent_storage_effect node.
Supports store, query, and distribution operations.
"""

from __future__ import annotations

import logging
from typing import Literal, Self
from uuid import UUID

from omnibase_core.models.intelligence import (
    ModelIntentClassificationOutput,
)
from omnimemory.utils.pii_detector import PIIDetector
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelIntentStorageRequest"]

logger = logging.getLogger(__name__)

# Module-level PII detector instance for validation
# Using medium sensitivity for user context to balance security and usability
_pii_detector = PIIDetector()


class ModelIntentStorageRequest(BaseModel):
    """Request model for intent storage operations.

    Supports three operation types:
    - store: Store a classified intent linked to a session
    - get_session: Retrieve intents for a specific session
    - get_distribution: Get aggregate intent category distribution

    Attributes:
        operation: The operation to perform.
        session_id: Session identifier (required for store and get_session).
        intent_data: Intent classification data (required for store).
        correlation_id: Optional correlation ID for request tracing.
        min_confidence: Minimum confidence threshold for filtering (queries).
        limit: Maximum results to return (queries).
        time_range_hours: Lookback period for distribution queries.
        user_context: Optional user context string for storage.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
    )

    operation: Literal["store", "get_session", "get_distribution"] = Field(
        ...,
        description="Operation type: 'store', 'get_session', or 'get_distribution'",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        description="Session identifier (required for store and get_session)",
    )
    intent_data: ModelIntentClassificationOutput | None = Field(
        default=None,
        description="Intent classification output (required for store)",
    )
    correlation_id: UUID | None = Field(
        default=None,
        description="Correlation ID for request tracing",
    )
    min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum confidence threshold for filtering",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum results to return",
    )
    time_range_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="Lookback period in hours for distribution queries",
    )
    user_context: str = Field(
        default="",
        max_length=10000,
        description="Optional user context for storage operations",
    )

    @model_validator(mode="after")
    def validate_user_context_pii(self) -> Self:
        """Validate user_context does not contain PII.

        User-facing input must be checked for PII before processing/storage
        to ensure compliance with privacy regulations.

        Raises:
            ValueError: If PII is detected in user_context.
        """
        if self.user_context:
            # Use medium sensitivity to balance security and usability
            result = _pii_detector.detect_pii(
                self.user_context, sensitivity_level="medium"
            )
            if result.has_pii:
                pii_types = ", ".join(t.value for t in result.pii_types_detected)
                logger.warning(
                    "PII detected in user_context",
                    extra={
                        "pii_types_detected": pii_types,
                        "operation": self.operation,
                    },
                )
                msg = (
                    f"user_context contains PII ({pii_types}). "
                    "Please remove sensitive information before submission."
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        """Validate that required fields are present for each operation type."""
        if self.operation == "store":
            if self.session_id is None:
                msg = "session_id is required for store operation"
                raise ValueError(msg)
            if self.intent_data is None:
                msg = "intent_data is required for store operation"
                raise ValueError(msg)
        elif self.operation == "get_session":
            if self.session_id is None:
                msg = "session_id is required for get_session operation"
                raise ValueError(msg)
        # get_distribution doesn't require session_id
        return self
