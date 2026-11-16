# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review key credit probe's overlay (OMN-24656): which tenant, model and host row the scheduled probe reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReviewKeyProbeOverlay(BaseModel):
    """The node's overlay document (``node_claude_review_gateway_effect/overlay.yaml`` under an overlay root).

    The scheduled unit holds none of these settings. ``host`` names this machine's lab host table row and, when
    absent, the probe command resolves it itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(
        min_length=1, description="The tenant whose review keys are probed."
    )
    model: str = Field(
        min_length=1, description="The Claude model each probe call names."
    )
    host: str | None = Field(
        default=None, min_length=1, description="This machine's lab host table row."
    )
