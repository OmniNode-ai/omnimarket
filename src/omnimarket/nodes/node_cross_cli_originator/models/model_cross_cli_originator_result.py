# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the cross-CLI originator node."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCrossCliOriginatorResult(BaseModel):
    """Published event_id and correlation_id for the caller to track."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(..., description="Event ID assigned by the emit daemon.")
    correlation_id: UUID = Field(..., description="Tracing correlation ID.")
    topic: str = Field(..., description="Kafka topic the envelope was published to.")


__all__: list[str] = ["ModelCrossCliOriginatorResult"]
