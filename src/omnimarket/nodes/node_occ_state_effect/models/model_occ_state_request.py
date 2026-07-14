# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccStateRequest — the command that triggers the RSD-2 read-EFFECT.

Identifies the product PR to gather state for; everything else the COMPUTE
node needs (:class:`~omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request.ModelOccCompanionRequest`)
is derived from live GitHub facts by :class:`HandlerOccStateEffect`, never
supplied here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelOccStateRequest(BaseModel):
    """Identify the product PR (and OCC repo) to gather companion state for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug (owner/repo).")
    pr_number: int = Field(..., description="Product PR number.")
    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    runner: str = Field(
        default="node_occ_companion_compute", description="Receipt runner identity."
    )
    verifier: str = Field(
        default="occ-evidence-source-autobind",
        description="Receipt verifier identity (must differ from runner).",
    )
    correlation_id: UUID = Field(default_factory=uuid4)


__all__ = ["ModelOccStateRequest"]
