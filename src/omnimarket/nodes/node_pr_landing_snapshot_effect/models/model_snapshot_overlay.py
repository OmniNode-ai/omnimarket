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
    "record_max_bytes",
)
_CONTROLLER_FIELDS = (
    "controller_facts_path",
    "controller_ticks_path",
    "controller_decisions_path",
    "controller_max_age_seconds",
    "controller_sidecar_path",
    "external_blocker_kinds",
    "external_blocker_unbounded_kinds",
    "external_gate_hours",
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
    record_max_bytes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Redpanda only: the largest tick record (its JSON, in bytes) the step publishes. The broker "
            "refuses a payload over its limit (1,048,588 bytes on the dev lane), so the overlay sets this "
            "below it; a record over it ends in the typed failed event record_too_large, never a "
            "refused publish."
        ),
    )
    controller_facts_path: str | None = Field(
        default=None,
        description=(
            "Cutover only: the landing controller's facts file, read with its tick and "
            "decision log and carried on the tick record (OMN-23665); set with its three "
            "companions, never alone."
        ),
    )
    controller_ticks_path: str | None = Field(
        default=None,
        description=(
            "Cutover only: the controller's tick log, one finished tick per line; its last "
            "row must be the facts file's tick (OMN-23665); set with its three companions, "
            "never alone."
        ),
    )
    controller_decisions_path: str | None = Field(
        default=None,
        description=(
            "Cutover only: the controller's decision log; a missing file is an empty log, "
            "and its rows for the facts file's tick ride on the carry (OMN-23665); set with "
            "its three companions, never alone."
        ),
    )
    controller_max_age_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Cutover only: a controller state older than this is refused (OMN-23665); set "
            "with its three companions, never alone."
        ),
    )

    controller_sidecar_path: str | None = Field(
        default=None,
        description=(
            "Cutover only: the controller's sidecar file; its external_judged (the external blockers a worker "
            "judged per pull request and head) is filled onto the pull requests' facts (OMN-22921); set with "
            "its companions, never alone. An unreadable file is an unread controller state, never an empty one."
        ),
    )
    external_blocker_kinds: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Cutover only: the blocker kinds that hold a pull request from a worker while the ref stands; "
            "filled into the facts' policy (OMN-22921)."
        ),
    )
    external_blocker_unbounded_kinds: tuple[str, ...] | None = Field(
        default=None,
        description="Cutover only: the kinds that hold with no time bound while the ref is open (OMN-22921).",
    )
    external_gate_hours: float | None = Field(
        default=None,
        gt=0,
        description="Cutover only: how long any other judged kind holds a pull request from a worker (OMN-22921).",
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

    @model_validator(mode="after")
    def _controller_fields_come_as_a_set(self) -> ModelSnapshotOverlay:
        set_fields = [
            name
            for name in _CONTROLLER_FIELDS
            if getattr(self, name) is not None and getattr(self, name) != ""
        ]
        if set_fields and len(set_fields) != len(_CONTROLLER_FIELDS):
            missing = [name for name in _CONTROLLER_FIELDS if name not in set_fields]
            raise ValueError(
                f"the controller carry fields come as a set; set with "
                f"{', '.join(set_fields)}: {', '.join(missing)}"
            )
        return self


__all__: list[str] = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "EnumSnapshotBackend",
    "ModelSnapshotOverlay",
]
