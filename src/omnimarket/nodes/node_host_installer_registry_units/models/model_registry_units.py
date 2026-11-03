# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the registry module tick unit installer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRegistryUnitsRequest(BaseModel):
    """Install, render or inspect one registry_module_tick unit.

    ``project_path`` is the unit's project path override: a checkout of the unit's registry project (a branch
    worktree) that the rendered unit runs in place of the canonical clone ``registry_root/<registry_project>``.
    ``expected_revision`` is the commit that checkout must be at. ``path_overrides`` replaces the value of the
    environment variables the unit contract lists as path-overridable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: Literal["dry-run", "install", "status", "kickstart", "uninstall"] = "dry-run"
    home: str
    registry_root: str
    workspace_config_root: str = ""
    plans_path: str = ""
    marketplace_root: str = ""
    project_path: str = ""
    expected_revision: str = ""
    path_overrides: dict[str, str] = Field(default_factory=dict)


class ModelRegistryUnitsResult(BaseModel):
    """``status`` is ``success`` or ``failure``; ``rendered`` is the plist text a dry-run or install produced;
    ``project`` and ``revision`` are the project the unit runs and the commit it is at; ``project_is_canonical`` says
    whether that project is the canonical registry clone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: str
    status: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    rendered: str = ""
    project: str = ""
    revision: str = ""
    project_is_canonical: bool = True
    record: str = ""
