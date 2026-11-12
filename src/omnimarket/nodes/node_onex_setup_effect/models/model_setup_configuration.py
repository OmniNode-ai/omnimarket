# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Portable, non-secret setup choices reuse the authoritative plan schema."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .model_setup_plan import ModelSetupPlan


class ModelSetupConfigurationProfile(BaseModel):
    """A named plan is desired configuration, never proof of applied effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    plan: ModelSetupPlan


class ModelSetupConfigurationRequest(BaseModel):
    """Explicit configuration operations; imports never run setup effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["validate", "export", "import", "list"]
    source: Path | None = None
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    replace: bool = False


class ModelSetupConfigurationResult(BaseModel):
    """Secret-free CLI result with the authority of the displayed choices."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ready", "unconfigured", "refused"]
    profile: ModelSetupConfigurationProfile | None = None
    source: Path | None = None
    profiles: tuple[str, ...] = ()
    changed: bool = False
    message: str | None = None
