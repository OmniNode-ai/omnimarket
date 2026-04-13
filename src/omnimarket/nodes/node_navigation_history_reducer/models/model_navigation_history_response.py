# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Response model for the navigation_history_reducer node.

.. versionadded:: 0.4.0
    Initial implementation for OMN-2584 Navigation History Storage.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelNavigationHistoryResponse(BaseModel):
    """Response from recording a navigation session.

    Since writes are fire-and-forget from the caller's perspective, this
    response is only observed by tests and internal monitoring — the caller
    does not await it.

    Attributes:
        session_id: The session identifier that was recorded (or skipped if
            idempotent).
        status: Outcome of the record operation.
        postgres_written: Whether the PostgreSQL row was written.
        qdrant_written: Whether the Qdrant point was written (success paths only).
        idempotent_skip: True if the session_id already existed and the write
            was a no-op.
        error_message: Human-readable error description if status is "error".
    """

    session_id: UUID = Field(description="Session identifier recorded")
    status: Literal["success", "error", "skipped"] = Field(
        description="Outcome of the record operation"
    )
    postgres_written: bool = Field(
        default=False,
        description="Whether the PostgreSQL row was written",
    )
    qdrant_written: bool = Field(
        default=False,
        description="Whether the Qdrant point was written (success paths only)",
    )
    idempotent_skip: bool = Field(
        default=False,
        description="True if session_id already existed (no-op write)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description if status is 'error'",
    )

    model_config = ConfigDict(frozen=True)
