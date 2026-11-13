# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gateway-instance settings the submit handler needs at runtime."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowSubmitSettings(BaseModel):
    """Frozen per-gateway-instance settings for the workflow submit handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gateway_instance_id: str = Field(..., min_length=1)
    envelope_schema_version: str = Field(..., min_length=1)
    default_refill_per_minute: int = Field(..., ge=1)
    default_burst_capacity: int = Field(..., ge=1)


__all__: list[str] = ["ModelWorkflowSubmitSettings"]
