# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona storage operations."""

from typing import Literal
from uuid import UUID

from omnimemory.models.persona import ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaStorageRequest(BaseModel):
    """Request to store a persona snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["store"] = Field(
        default="store",
        description="Storage operation (currently only 'store')",
    )
    persona: ModelUserPersonaV1 = Field(
        ...,
        description="Persona snapshot to persist",
    )
    trigger_reason: Literal["tick", "on_demand"] = Field(
        ...,
        description="What triggered this storage operation",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation ID for tracing",
    )
