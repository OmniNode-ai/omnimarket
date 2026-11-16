# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot step's deployment overlay: every setting, no code default for a deployment fact (OMN-23664)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_work_ledger_reader import ModelCommandReadRoute
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

OVERLAY_ID = "node_lane_host_pool_snapshot_effect"
# A second instance of the step pins its own overlay file here;
# unset, the overlay is <root>/node_lane_host_pool_snapshot_effect/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS.
OVERLAY_FILE_ENV = "ONEX_POOL_SNAPSHOT_OVERLAY_FILE"
_REDPANDA_FIELDS = (
    "snapshot_path",
    "advertisements",
    "accounts",
    "emission_journal",
)


class EnumPoolSnapshotBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelAdvertisementRoute(BaseModel):
    """How host advertisements are read: from a file or a command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["file", "command"] = Field(
        description="The advertisement source kind: a file to read, or a command to run."
    )
    path: str | None = Field(
        default=None,
        description="The file route: the JSON list of host advertisements to read.",
    )
    argv: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "The command route: the argv to run; stdout is a JSON list of host "
            "advertisements (`onex lab-work hosts --json`)."
        ),
    )
    timeout_seconds: int = Field(
        ge=1,
        description="The command route: the seconds before the run is refused.",
    )

    @model_validator(mode="after")
    def _route_needs_its_field(self) -> ModelAdvertisementRoute:
        needed = {"file": self.path, "command": self.argv}
        if not needed[self.route]:
            field_name = {"file": "path", "command": "argv"}[self.route]
            raise ValueError(f"the {self.route} route needs {field_name}")
        return self


class ModelAccountsRoute(BaseModel):
    """How a host's token pool accounts are read: a local or remote command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_argv: tuple[str, ...] = Field(
        min_length=1,
        description="The argv run on the local host for a local row.",
    )
    remote_argv: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "The argv run for a remote row; `{target}` is substituted with the "
            "host row's ssh target."
        ),
    )
    timeout_seconds: int = Field(
        ge=1,
        description="The seconds before a run is refused.",
    )


class ModelPoolSnapshotOverlay(BaseModel):
    """<root>/node_lane_host_pool_snapshot_effect/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumPoolSnapshotBackend = Field(
        description="Where the snapshot goes: kept in memory, or published on Redpanda."
    )
    snapshot_topic: str = Field(
        min_length=1,
        description="The topic the snapshot record is published on.",
    )
    failed_topic: str = Field(
        min_length=1, description="The topic a typed snapshot failure is published on."
    )
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    advertisement_max_age_seconds: int = Field(
        ge=1,
        description="A host advertisement older than this is stale, never read.",
    )
    snapshot_max_age_seconds: int = Field(
        ge=1,
        description="The freshness bound every reader of the snapshot applies.",
    )
    cores_per_lane: int = Field(
        ge=1,
        description=(
            "A host row with no declared cap has lane cap = max(1, cores // cores_per_lane)."
        ),
    )
    engines: tuple[str, ...] = Field(
        min_length=1,
        description="The engines a host with a usable account can run.",
    )
    codex_engine: str = Field(
        description="Added to engines when the host row has codex true."
    )
    ledger_lookback_hours: float = Field(
        gt=0,
        description=(
            "How far back the CLAIM, TERMINAL and RELEASE rows are read: a CLAIM older than this is not a running lane. "
            "The bus read command refuses an answer past its byte bound, so this is also the bound of one read."
        ),
    )
    record_suffix: str = Field(
        min_length=1,
        description="The suffix of the record file beside the snapshot file.",
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
    snapshot_path: str | None = Field(
        default=None,
        description="Redpanda only: the snapshot file the record is written beside.",
    )
    advertisements: ModelAdvertisementRoute | None = Field(
        default=None,
        description="Redpanda only: how host advertisements are read.",
    )
    ledger_read: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute | None = Field(
        default=None,
        discriminator="route",
        description="Redpanda only: how the ledger's lane rows are read (the reading writer's route: a file or the bus read command).",
    )
    open_claims_read: ModelCommandReadRoute | None = Field(
        default=None,
        description=(
            "Redpanda only, in place of ledger_read: the running lanes are the ledger node's open claims since the lookback, one point "
            "query through the bus read command named by command_env (the rows read of ledger_read is over the node's answer bound)."
        ),
    )
    accounts: ModelAccountsRoute | None = Field(
        default=None,
        description="Redpanda only: how a host's token pool accounts are read.",
    )
    emission_journal: str | None = Field(
        default=None,
        description="Redpanda only: the floor's emission journal each pass's chain is written to.",
    )

    @model_validator(mode="after")
    def _redpanda_needs_its_fields(self) -> ModelPoolSnapshotOverlay:
        if self.backend is EnumPoolSnapshotBackend.REDPANDA:
            missing = [name for name in _REDPANDA_FIELDS if not getattr(self, name)]
            if missing:
                raise ValueError(f"the redpanda backend needs {', '.join(missing)}")
            if (self.ledger_read is None) == (self.open_claims_read is None):
                raise ValueError(
                    "the redpanda backend names one of ledger_read or open_claims_read"
                )
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
    "EnumPoolSnapshotBackend",
    "ModelAccountsRoute",
    "ModelAdvertisementRoute",
    "ModelPoolSnapshotOverlay",
]
