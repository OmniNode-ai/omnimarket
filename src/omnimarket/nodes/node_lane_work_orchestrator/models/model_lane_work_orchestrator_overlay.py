# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's overlay values: inputs, never constants in a handler (OMN-26721)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)

_EXAMPLE = Path(__file__).resolve().parent.parent / "overlay.example.yaml"


class ModelLaneWorkOrchestratorOverlay(BaseModel):
    """Proof commands by repository, the requesting host and lane, the proof host, and the
    reducer's configuration. The overlay file is the bootstrap source; the settings store
    owns it once it holds the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    reducer: ModelLaneWorkReducerConfig = Field(
        default_factory=ModelLaneWorkReducerConfig
    )


def load_orchestrator_overlay(
    path: Path | None = None,
) -> ModelLaneWorkOrchestratorOverlay:
    """Read the overlay file (the bootstrap source) into the model."""
    text = (path or _EXAMPLE).read_text(encoding="utf-8")  # node-purity-ok: own overlay
    return ModelLaneWorkOrchestratorOverlay.model_validate(yaml.safe_load(text))


__all__: list[str] = ["ModelLaneWorkOrchestratorOverlay", "load_orchestrator_overlay"]
