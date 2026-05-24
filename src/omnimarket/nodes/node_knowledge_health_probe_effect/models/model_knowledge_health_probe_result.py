# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the knowledge health probe effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events.knowledge_health import ModelKnowledgeBackendProbe


class ModelKnowledgeHealthProbeResult(BaseModel):
    """Raw backend probe results collected by the effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_probes: tuple[ModelKnowledgeBackendProbe, ...]


__all__ = ["ModelKnowledgeHealthProbeResult"]
