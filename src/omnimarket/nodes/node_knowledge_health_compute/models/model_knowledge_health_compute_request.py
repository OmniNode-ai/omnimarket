# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the knowledge health compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_backend_probe import (
    ModelKnowledgeBackendProbe,
)


class ModelKnowledgeHealthComputeRequest(BaseModel):
    """Collected backend probe results to classify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_probes: tuple[ModelKnowledgeBackendProbe, ...]


__all__ = ["ModelKnowledgeHealthComputeRequest"]
