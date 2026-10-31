# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's ``runtime`` block and the deployment overlay the runtime entry reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFactoryFloorSources(BaseModel):
    """Where each source is, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_relpath: str = Field(min_length=1)
    plans_today_dir: str = Field(min_length=1)
    plans_today_file: str = Field(min_length=1)
    linear_snapshot_glob: str = Field(min_length=1)
    placement_relpath: str = Field(min_length=1)
    friction_report_relpath: str = Field(min_length=1)


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
    watcher_max_age_min: int = Field(gt=0)
    manifest_suffix: str = Field(min_length=1)
    plans_relpath: str = Field(min_length=1)
    sources: ModelFactoryFloorSources
    output: ModelFactoryFloorOutput


class ModelFactoryFloorEffectContract(BaseModel):
    """The ``runtime`` block is the only part of this contract the handler reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorRuntime


class ModelFactoryFloorOverlay(BaseModel):
    """Deployment choice: which plans are lines. There is no packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = Field(min_length=1)
