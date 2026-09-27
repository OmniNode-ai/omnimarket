# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's input: exactly one of the four landing events.

The payload classes are the frozen T2 seam (OMN-19824), imported rather than
restated, so a field the orchestrator adds or renames breaks this projection at
validation instead of being silently dropped. Those classes forbid extra
fields; the writer strips the runtime's underscore-prefixed injections before
building this request.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.model_pr_landing_agent_needed import (
    ModelPrLandingAgentNeeded,
)
from omnimarket.events.pr_landing.model_pr_landing_closed import (
    ModelPrLandingClosed,
)
from omnimarket.events.pr_landing.model_pr_landing_merged import (
    ModelPrLandingMerged,
)
from omnimarket.events.pr_landing.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_projection_pr_landing.models.enum_pr_landing_projection_event_kind import (
    EnumPrLandingProjectionEventKind,
)


class ModelPrLandingProjectionRequest(BaseModel):
    """One landing event, in the field named for its kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transitioned: ModelPrLandingTransitioned | None = Field(default=None)
    agent_needed: ModelPrLandingAgentNeeded | None = Field(default=None)
    merged: ModelPrLandingMerged | None = Field(default=None)
    closed: ModelPrLandingClosed | None = Field(default=None)

    @model_validator(mode="after")
    def _exactly_one_event(self) -> Self:
        present = [
            name
            for name in ("transitioned", "agent_needed", "merged", "closed")
            if getattr(self, name) is not None
        ]
        if len(present) != 1:
            msg = f"exactly one landing event per request, got {present or 'none'}"
            raise ValueError(msg)
        return self

    @property
    def event_kind(self) -> EnumPrLandingProjectionEventKind:
        if self.transitioned is not None:
            return EnumPrLandingProjectionEventKind.TRANSITIONED
        if self.agent_needed is not None:
            return EnumPrLandingProjectionEventKind.AGENT_NEEDED
        if self.merged is not None:
            return EnumPrLandingProjectionEventKind.MERGED
        return EnumPrLandingProjectionEventKind.CLOSED


__all__: list[str] = ["ModelPrLandingProjectionRequest"]
