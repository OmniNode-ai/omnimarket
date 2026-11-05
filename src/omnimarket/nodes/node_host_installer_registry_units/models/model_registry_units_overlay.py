# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment-supplied launchd units, consumed by the existing registry installer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRegistryUnitSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_interval: int = Field(gt=0)
    run_at_load: bool
    description: str


class ModelRegistryUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Literal["launchd"]
    kind: Literal["registry_module_tick"]
    label: str = Field(min_length=1)
    template: str
    schedule: ModelRegistryUnitSchedule
    registry_project: str
    interpreter_candidates: tuple[str, ...]
    module: str
    entry_file: str
    project_path_override: dict[str, str] = Field(default_factory=dict)
    environment_path_overrides: tuple[str, ...] = ()
    environment: dict[str, str] = Field(default_factory=dict)
    arguments: tuple[str, ...] = ()
    evidence_file: str
    state_directory: str


class ModelRegistryUnitsOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    units: dict[str, ModelRegistryUnit]
