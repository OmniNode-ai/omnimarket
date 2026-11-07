# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's deployment overlay: every setting, no code default for a deployment fact."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_pr_landing_tick_orchestrator"


class EnumTickBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelTickOrchestratorOverlay(BaseModel):
    """<root>/node_pr_landing_tick_orchestrator/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumTickBackend = Field(
        description="Where recorded tick snapshots are read from."
    )
    act_enabled: bool = Field(
        description="Whether act mode may call the writer. False while the script keeps landing."
    )
    max_snapshots: int = Field(ge=1, description="Snapshots compared in one run.")
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    emission_journal: str | None = Field(
        default=None, description="Redpanda profile only: the emission journal path."
    )
    bootstrap_servers: str | None = Field(default=None, description="Redpanda only.")
    consumer_group: str | None = Field(default=None, description="Redpanda only.")
    snapshot_topic: str | None = Field(
        default=None,
        description="Redpanda only: the topic the script's tick records arrive on.",
    )
    poll_timeout_s: float | None = Field(
        default=None, gt=0, description="Redpanda only."
    )


__all__: list[str] = ["OVERLAY_ID", "EnumTickBackend", "ModelTickOrchestratorOverlay"]
