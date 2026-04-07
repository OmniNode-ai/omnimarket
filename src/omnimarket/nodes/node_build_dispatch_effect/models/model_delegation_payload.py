# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDelegationPayload — delegation request for orchestrator publishing.

Effect handlers must not publish events directly — they return payloads
for the orchestrator to publish (architectural rule: only orchestrators
may access the event bus).

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationPayload(BaseModel):
    """A delegation request payload to be published by the orchestrator.

    Effect handlers must not publish events directly — they return payloads
    for the orchestrator to publish (architectural rule: only orchestrators
    may access the event bus).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(..., description="Logical event type for routing.")
    topic: str = Field(..., description="Kafka topic to publish to.")
    # ONEX_EXCLUDE: any_type - delegation payloads carry arbitrary JSON data from
    # various upstream sources (prompt params, task metadata) whose schema is unknown
    # at compile time; a strict union type would mirror the full upstream message spec.
    payload: dict[str, Any] = Field(..., description="JSON-serialisable event payload.")
    correlation_id: UUID = Field(..., description="Tracing correlation ID.")


__all__: list[str] = ["ModelDelegationPayload"]
