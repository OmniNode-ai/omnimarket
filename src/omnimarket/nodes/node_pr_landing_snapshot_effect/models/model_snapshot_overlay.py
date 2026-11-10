# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's deployment overlay: every setting, no code default for a deployment fact (OMN-23409)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

OVERLAY_ID = "node_pr_landing_snapshot_effect"
# A second instance of the step (the cutover repositories' unit, OMN-23514) pins its own overlay file here;
# unset, the overlay is <root>/node_pr_landing_snapshot_effect/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS.
OVERLAY_FILE_ENV = "ONEX_LANDING_SNAPSHOT_OVERLAY_FILE"
_REDPANDA_FIELDS = (
    "facts_path",
    "watcher_state_path",
    "watcher_max_age_seconds",
    "owner",
    "record_journal",
    "emission_journal",
)


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
    bus_lane: str | None = Field(
        default=None,
        description=(
            "Redpanda only: the workspace bus lane records are published on (dev); the broker and "
            "client transport resolve from the lane config under workspace_root."
        ),
    )
    workspace_root: str | None = Field(
        default=None,
        description="Redpanda with bus_lane: the workspace root whose lane config names the broker.",
    )
    bootstrap_servers: str | None = Field(
        default=None,
        description="Redpanda only, in place of bus_lane: a plaintext lab broker started for a proof.",
    )
    facts_path: str | None = Field(
        default=None,
        description=(
            "Redpanda only: the landing controller's facts file for its last tick, the declared "
            "facts source until the node path ports the controller's Snapshot functions."
        ),
    )
    watcher_state_path: str | None = Field(
        default=None,
        description="Redpanda only: the PR watcher's state, the declared write-facts source.",
    )
    watcher_max_age_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Redpanda only: a watcher state older than this is refused, never read.",
    )
    owner: str | None = Field(
        default=None,
        min_length=1,
        description="Redpanda only: the owner the watcher's repo#n keys are under.",
    )
    record_journal: str | None = Field(
        default=None,
        description="Redpanda only: the journal every tick's record is appended to.",
    )
    emission_journal: str | None = Field(
        default=None,
        description="Redpanda only: the floor's emission journal each pass's chain is written to.",
    )

    @model_validator(mode="after")
    def _redpanda_needs_its_fields(self) -> ModelSnapshotOverlay:
        if self.backend is EnumSnapshotBackend.REDPANDA:
            missing = [name for name in _REDPANDA_FIELDS if not getattr(self, name)]
            if missing:
                raise ValueError(f"the redpanda backend needs {', '.join(missing)}")
            if bool(self.bus_lane) == bool(self.bootstrap_servers):
                raise ValueError(
                    "the redpanda backend names one of bus_lane or bootstrap_servers"
                )
            if self.bus_lane and not self.workspace_root:
                raise ValueError("bus_lane needs workspace_root")
        return self


__all__: list[str] = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "EnumSnapshotBackend",
    "ModelSnapshotOverlay",
]
