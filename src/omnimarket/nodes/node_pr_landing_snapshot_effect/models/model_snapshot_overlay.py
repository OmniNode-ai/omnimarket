# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's deployment overlay: every setting, no code default for a deployment fact (OMN-23409)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

OVERLAY_ID = "node_pr_landing_snapshot_effect"


class EnumSnapshotBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelSnapshotOverlay(BaseModel):
    """<root>/node_pr_landing_snapshot_effect/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumSnapshotBackend = Field(
        description="Where tick records go: kept in memory, or published on Redpanda."
    )
    record_topic: str = Field(
        min_length=1,
        description=(
            "The topic each tick's record is published on; the tick orchestrator's "
            "snapshot_topic reads it."
        ),
    )
    failed_topic: str = Field(
        min_length=1, description="The topic a typed snapshot failure is published on."
    )
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    bootstrap_servers: str | None = Field(default=None, description="Redpanda only.")

    @model_validator(mode="after")
    def _redpanda_needs_servers(self) -> ModelSnapshotOverlay:
        if self.backend is EnumSnapshotBackend.REDPANDA and not self.bootstrap_servers:
            raise ValueError("the redpanda backend needs bootstrap_servers")
        return self


__all__: list[str] = ["OVERLAY_ID", "EnumSnapshotBackend", "ModelSnapshotOverlay"]
