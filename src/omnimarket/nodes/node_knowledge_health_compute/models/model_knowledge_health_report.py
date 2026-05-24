# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the knowledge health compute node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_backend_status import (
    ModelKnowledgeBackendStatus,
)


class ModelKnowledgeHealthReport(BaseModel):
    """Aggregated knowledge health report from classified backend probes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_status: Literal["healthy", "degraded", "unhealthy"]
    backend_statuses: tuple[ModelKnowledgeBackendStatus, ...]
    recommendations: tuple[str, ...]


__all__ = ["ModelKnowledgeHealthReport"]
