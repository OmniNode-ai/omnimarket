# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's ``runtime`` block and the deployment overlay the runtime entry reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_factory_floor_input import (
    ModelFloorMachineDecl,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
)


class ModelFactoryFloorSources(BaseModel):
    """Where each source is, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_relpath: str = Field(min_length=1)
    plans_today_dir: str = Field(min_length=1)
    plans_today_file: str = Field(min_length=1)
    linear_snapshot_glob: str = Field(min_length=1)
    placement_relpath: str = Field(min_length=1)
    friction_report_relpath: str = Field(min_length=1)
    linear_titles_glob: str = Field(min_length=1)
    runner_fleet_relpath: str = Field(min_length=1)


class ModelFactoryFloorLaneSources(BaseModel):
    """Where the live-lane and machine-bay sources are (OMN-21733).

    ``*_env`` names an environment variable that, when set, gives the directory or file; else the ``*_fallback`` is
    taken relative to the user's home (``host_table_relpath`` relative to the registry home).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_lane_dir_env: str = Field(min_length=1)
    remote_lane_dir_fallback: str = Field(min_length=1)
    pool_receipt: str = Field(min_length=1)
    receipt_glob: str = Field(min_length=1)
    receipt_window_hours: int = Field(gt=0)
    result_chars: int = Field(gt=0)
    placement_dir_env: str = Field(min_length=1)
    placement_dir_fallback: str = Field(min_length=1)
    lane_marker_min_id: int = Field(gt=0)
    cloud_accounts_file: str = Field(min_length=1)
    host_table_env: str = Field(min_length=1)


class ModelFactoryFloorContainerSource(BaseModel):
    """How a host's container listing is read: ssh to the overlay's target, then the listing command (one JSON object per line)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_options: tuple[str, ...] = Field(min_length=1)
    # `docker ps` with an explicit field list: the default `{{json .}}` makes the daemon compute every container's size.
    listing_command: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelFactoryFloorOutput(BaseModel):
    """The path the JSON is written to, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str = Field(min_length=1)


class ModelFactoryFloorRuntime(BaseModel):
    """The ``runtime`` block of contract.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    plans_kb_env: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    plugin_root_env: str = Field(min_length=1)
    ledger_window_days: int = Field(gt=0)
    ledger_cut_chars: int = Field(gt=0)
    watcher_max_age_min: int = Field(gt=0)
    manifest_suffix: str = Field(min_length=1)
    plan_header_keys: tuple[str, ...] = Field(min_length=1)
    plans_relpath: str = Field(min_length=1)
    sources: ModelFactoryFloorSources
    lane_sources: ModelFactoryFloorLaneSources
    container_source: ModelFactoryFloorContainerSource
    output: ModelFactoryFloorOutput


class ModelFactoryFloorEffectContract(BaseModel):
    """The ``runtime`` block is the only part of this contract the handler reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorRuntime


class ModelFactoryFloorPlanRef(BaseModel):
    """A plan that is not on the plans repository's default branch yet: read from this ref until it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    ref: str = Field(min_length=1)


class ModelFactoryFloorTaskLinks(BaseModel):
    """The tickets that carry a plan's tasks, for a plan whose own text names none (a task id maps to its tickets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: str = Field(min_length=1)
    tasks: dict[str, tuple[str, ...]] = Field(min_length=1)


class ModelFactoryFloorContainerHost(BaseModel):
    """A machine whose lane containers are read: the name the budget declarations use and the ssh target that reaches it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)


class ModelFactoryFloorOverlay(BaseModel):
    """Deployment choice: which plans are lines and which machines are bays. There is no packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = Field(min_length=1)
    # The machines drawn as bays, in order. A machine not listed here is never drawn, whatever the pool read holds.
    machines: tuple[ModelFloorMachineDecl, ...] = ()
    # The shared lab host table (caps, repair), relative to the registry home, for a deployment where the host_table_env
    # variable is not set. Empty: the host table is unread unless the variable names it.
    host_table_relpath: str = ""
    # Plans on a branch that has not merged: a line drawn from the ref until the plan reaches the default branch.
    plan_refs: tuple[ModelFactoryFloorPlanRef, ...] = ()
    # Which tickets carry which task of a plan whose text names none.
    plan_task_links: tuple[ModelFactoryFloorTaskLinks, ...] = ()
    # The declared line (a plan_path_contains text) that shows each lane's application containers; empty shows none.
    container_read_line: str = ""
    container_hosts: tuple[ModelFactoryFloorContainerHost, ...] = ()
    container_budgets: tuple[ModelLaneContainerBudgetDeclaration, ...] = ()
