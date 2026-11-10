# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request of node_retrospective_run_orchestrator: the key fields of the crossing it consumes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRetrospectiveRunOrchestratorRequest(BaseModel):
    """Key fields of the consumed crossing; the envelope model is named in the contract seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    correlation_id: str


__all__ = ["ModelRetrospectiveRunOrchestratorRequest"]
