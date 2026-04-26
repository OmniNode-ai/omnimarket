# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation payload for dispatch-worker execution."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchWorkerDelegationPayload(BaseModel):
    """Publishable runtime delegation request.

    Effect handlers return payloads; orchestrators/runtime adapters publish them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(..., description="Logical event type for routing.")
    topic: str = Field(..., description="Kafka topic to publish to.")
    payload: dict[str, object] = Field(..., description="JSON-serializable payload.")
    correlation_id: UUID = Field(..., description="Tracing correlation ID.")


__all__ = ["ModelDispatchWorkerDelegationPayload"]
