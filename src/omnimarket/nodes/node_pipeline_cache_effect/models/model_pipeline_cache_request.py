# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPipelineCacheRequest — input to node_pipeline_cache_effect."""

from __future__ import annotations

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict

__all__ = ["ModelPipelineCacheRequest"]


class ModelPipelineCacheRequest(BaseModel):
    """Request the cached test + golden-chain artifacts for a ticket contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract
    generator_version: str = "1.0.0"
    generation_profile_hash: str = "profile_default"
    cache_root: str | None = None
