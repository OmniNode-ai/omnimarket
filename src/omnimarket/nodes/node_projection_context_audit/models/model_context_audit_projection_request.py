# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request to the context-audit fold: one event and where it was read from."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_context_audit.models.model_context_audit_event_wire import (
    ModelContextAuditEventWire,
)


class ModelContextAuditProjectionRequest(BaseModel):
    """The event plus the Kafka coordinates its row key is derived from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelContextAuditEventWire
    source_topic: str = Field(min_length=1)
    source_partition: int = Field(ge=0)
    source_offset: int = Field(ge=0)


__all__ = ["ModelContextAuditProjectionRequest"]
