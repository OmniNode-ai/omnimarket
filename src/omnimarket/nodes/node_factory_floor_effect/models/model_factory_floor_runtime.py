# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's ``runtime`` block and the deployment overlay the runtime entry reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_factory_floor_input import (
    ModelFloorMachineDecl,
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
    output: ModelFactoryFloorOutput


class ModelFactoryFloorEffectContract(BaseModel):
    """The ``runtime`` block is the only part of this contract the handler reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorRuntime


class ModelFactoryFloorOverlay(BaseModel):
    """Deployment choice: which plans are lines and which machines are bays. There is no packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = Field(min_length=1)
    # The machines drawn as bays, in order. A machine not listed here is never drawn, whatever the pool read holds.
    machines: tuple[ModelFloorMachineDecl, ...] = ()
    # The shared lab host table (caps, repair), relative to the registry home, for a deployment where the host_table_env
    # variable is not set. Empty: the host table is unread unless the variable names it.
    host_table_relpath: str = ""
