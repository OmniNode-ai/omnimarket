# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for the lane-end run."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneCloseoutRunRequest(BaseModel):
    """Close out every tree under a lane's directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    lane: str
    lane_dir: str
    run_id: str = Field(
        default="run", description="Joins the lane name into the WIP tag."
    )
    remove: bool = True
    claims_dir: str | None = Field(
        default=None, description="PR claim registry on the dispatch host."
    )
    session_id: str | None = Field(
        default=None, description="Ended engine's exact session identity."
    )
    claim_run_id: str | None = Field(
        default=None,
        description="Exact PR claim run identity, distinct from the transport/WIP run_id.",
    )
    engine_ended: bool = Field(
        default=False, description="Caller observed the engine exit before closeout."
    )
