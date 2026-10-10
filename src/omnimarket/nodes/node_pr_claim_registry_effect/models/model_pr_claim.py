# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One claim file, as the registry reads it back."""

from pydantic import BaseModel, ConfigDict, Field


class ModelPrClaim(BaseModel):
    """A claim record. Every field is optional: a legacy claim written before
    the lane and session fields existed, or one a person edited, still reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pr_key: str | None = Field(default=None, description="Canonical PR key.")
    claimed_by_run: str | None = Field(default=None, description="Holding run id.")
    claimed_by_host: str | None = Field(default=None, description="Holding host.")
    claimed_by_instance_id: str | None = Field(
        default=None, description="Stable instance id of the holding machine."
    )
    claimed_at: str | None = Field(default=None, description="UTC claim time.")
    last_heartbeat_at: str | None = Field(
        default=None, description="UTC time of the last heartbeat."
    )
    action: str | None = Field(default=None, description="What the holder is doing.")
    lane_id: str | None = Field(default=None, description="Owning lane handle.")
    claimed_by_session: str | None = Field(
        default=None, description="Full claiming session identity."
    )
