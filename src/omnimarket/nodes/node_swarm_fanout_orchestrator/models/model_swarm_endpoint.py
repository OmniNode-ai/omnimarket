# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSwarmEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str
    base_url: str
    model_id: str
    endpoint_ref: str = ""
    """Name of env var holding the base URL (e.g. 'LLM_LOCAL_PRIMARY_URL')."""
    status: str = "reachable"
    capabilities: tuple[str, ...] = ()
