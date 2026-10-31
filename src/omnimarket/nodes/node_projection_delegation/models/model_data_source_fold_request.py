# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request for the delegate-skill data_source fold (OMN-21536)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)


class ModelDataSourceFoldRequest(BaseModel):
    """A delegate-skill terminal and the envelope's data_source attribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    terminal: ModelDelegateSkillTerminalProjection = Field(
        ..., description="The delegate-skill terminal whose provenance is folded."
    )
    envelope_source: str = Field(
        ..., description="The data_source the projection envelope carried."
    )


__all__ = ["ModelDataSourceFoldRequest"]
