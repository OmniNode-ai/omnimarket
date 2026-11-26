# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the lane work orchestrator overlay through (OMN-26722).

The orchestrator's overlay holds the help chain (``help_tiers``) beside the settings task 6 reads. The settings store
owns the chain once it holds a document; ``overlay.example.yaml`` beside the node is the bootstrap file. No secret is
a setting here. A chain is ordered, non-empty, names each tier once and each tier one backend, harness, model and effort.
"""

from __future__ import annotations

from typing import Self

from omnibase_core.models.lane_work import ModelLaneHelpTier
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLaneWorkOrchestratorOverlay(BaseModel):
    """The orchestrator's overlay document; keys other than the chain belong to other readers."""

    model_config = ConfigDict(frozen=True, extra="allow")

    schema_version: int = Field(default=1, ge=1)
    help_tiers: tuple[ModelLaneHelpTier, ...] = Field(default=())

    @model_validator(mode="after")
    def _chain_is_usable(self) -> Self:
        names = [tier.name for tier in self.help_tiers]
        if len(set(names)) != len(names):
            raise ValueError("help_tiers names a tier twice")
        return self


__all__ = ["ModelLaneWorkOrchestratorOverlay"]
