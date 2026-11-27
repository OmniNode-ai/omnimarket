# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work orchestrator's one overlay document (OMN-26721, plan section 12.2).

One document, one model: the orchestrator's settings, the reducer's settings and the help
chain (``help_tiers``, top level, the ruled eight tiers in order). The overlay file beside
the node is the bootstrap source; the settings store owns the document once it holds one.
``reducer.help_tiers`` does not exist: the chain is a top-level key, and the orchestrator
passes it to the reducer as an input when a workflow starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from omnibase_core.models.lane_work import ModelLaneHelpTier
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    DEFAULT_DEADLINE_SECONDS,
    ModelLaneWorkReducerConfig,
)

_EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_lane_work_orchestrator"
    / "overlay.example.yaml"
)


class ModelLaneWorkReducerSettings(BaseModel):
    """The reducer's bounds: repeat bound, help budget, deadlines (wait budget included)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repeat_bound: int = Field(default=2, ge=1, description="Same failure twice.")
    help_budget: int = Field(default=3, ge=0, description="Help episodes per workflow.")
    deadline_seconds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_DEADLINE_SECONDS)
    )


class ModelLaneWorkOrchestratorOverlay(BaseModel):
    """Proof commands by repository, the requesting host and lane, the proof host, the
    reducer's bounds and the help chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    requesting_host: str = Field(..., min_length=1)
    requesting_lane: str = Field(..., min_length=1)
    proof_target_host: str = Field(
        ..., min_length=1, description="Where a proof runs when placement names none."
    )
    default_wait_seconds: int = Field(default=60, ge=1)
    decision_owner: str = Field(default="orchestrator session", min_length=1)
    proof_commands: dict[str, tuple[str, ...]] = Field(
        ..., description="Repository name (or default) to the proof argv."
    )
    reducer: ModelLaneWorkReducerSettings = Field(
        default_factory=ModelLaneWorkReducerSettings
    )
    help_tiers: tuple[ModelLaneHelpTier, ...] = Field(
        default=(), description="The responder chain, rank one first."
    )

    @model_validator(mode="after")
    def _chain_is_usable(self) -> Self:
        names = [tier.name for tier in self.help_tiers]
        if len(set(names)) != len(names):
            raise ValueError("help_tiers names a tier twice")
        return self

    def reducer_config(self) -> ModelLaneWorkReducerConfig:
        """The reducer's input: its bounds with the chain, copied into a workflow at start."""
        return ModelLaneWorkReducerConfig(
            repeat_bound=self.reducer.repeat_bound,
            help_budget=self.reducer.help_budget,
            deadline_seconds=dict(self.reducer.deadline_seconds),
            help_tiers=self.help_tiers,
        )


def load_orchestrator_overlay(
    path: Path | None = None,
) -> ModelLaneWorkOrchestratorOverlay:
    """Read the overlay file (the bootstrap source) into the model."""
    text = (path or _EXAMPLE).read_text(encoding="utf-8")
    return ModelLaneWorkOrchestratorOverlay.model_validate(yaml.safe_load(text))


__all__: list[str] = [
    "ModelLaneWorkOrchestratorOverlay",
    "ModelLaneWorkReducerSettings",
    "load_orchestrator_overlay",
]
