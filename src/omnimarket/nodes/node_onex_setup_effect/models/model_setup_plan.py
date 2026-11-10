# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic model for the model setup plan."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .model_setup_provider import ModelSetupProvider


class ModelSetupPlan(BaseModel):
    """The model setup plan describing what to install and where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path
    action: Literal["install", "verify", "repair", "configure", "upgrade"] = "install"
    components: tuple[
        Literal["delegation", "dashboard", "claude-plugin", "claude-hooks", "docker"],
        ...,
    ] = ("delegation",)
    providers: tuple[ModelSetupProvider, ...] = ()
    developer: bool = False

    @field_validator("workspace")
    @classmethod
    def _validate_workspace(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @field_validator("components")
    @classmethod
    def _validate_components(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("components must be non-empty")
        if len(set(v)) != len(v):
            raise ValueError("components must be unique")
        if "claude-hooks" in v and "claude-plugin" not in v:
            raise ValueError("claude-hooks requires claude-plugin")
        return v

    @field_validator("providers")
    @classmethod
    def _validate_providers(
        cls, v: tuple[ModelSetupProvider, ...]
    ) -> tuple[ModelSetupProvider, ...]:
        names = [p.provider for p in v]
        if len(set(names)) != len(names):
            raise ValueError("providers must be unique")
        return v
