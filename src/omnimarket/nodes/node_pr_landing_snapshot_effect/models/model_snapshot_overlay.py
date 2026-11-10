# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's deployment overlay: every setting, no code default for a deployment fact."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_pr_landing_snapshot_effect"


class EnumSnapshotBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelSnapshotOverlay(BaseModel):
    """<root>/node_pr_landing_snapshot_effect/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumSnapshotBackend = Field(
        description=(
            "inmemory: the watcher records and the topic log are injected. redpanda: the "
            "watcher state file and the bus named below."
        )
    )
    failed_conclusions: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "The check conclusions whose workflow runs the GitHub landing effect's rerun "
            "takes as failed (rerun-failed-jobs)."
        ),
    )
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    watcher_state_path: str | None = Field(
        default=None,
        description="Redpanda only: the PR watcher's state file the node id and red runs are read from.",
    )
    bootstrap_servers: str | None = Field(
        default=None,
        description="Redpanda only: the broker the snapshot records are published to.",
    )
    emission_journal: str | None = Field(
        default=None, description="Redpanda only: the emission journal path."
    )


__all__: list[str] = ["OVERLAY_ID", "EnumSnapshotBackend", "ModelSnapshotOverlay"]
