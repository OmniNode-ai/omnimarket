# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, step outcome, failure and result of the onboarding walk behind ``onex setup``."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelOnexSetupRequest(BaseModel):
    """What the walk was given: the values it was told, and where the user's ONEX home is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path | None = Field(
        default=None,
        description="Workspace root (OMNIBASE_PATH). None when nothing named one and no prompt ran.",
    )
    install_path: str | None = Field(
        default=None,
        description="local, docker or cloud as given; None keeps the recorded path, else local.",
    )
    onex_home: Path = Field(
        description="The user's ONEX home (~/.onex); config.yaml and setup.json live here."
    )


class ModelOnexSetupStepOutcome(BaseModel):
    """One completed step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    changed: bool = Field(description="True when the step wrote or created something.")
    detail: str


class ModelOnexSetupFailure(BaseModel):
    """The step that stopped the walk, with its one fix and the command to retry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    problem: str
    fix: str
    retry: str = Field(
        description="The full onex setup command to run once the fix is applied."
    )


class ModelOnexSetupState(BaseModel):
    """What a completed walk records in <onex_home>/setup.json for the next run to derive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path
    install_path: str


class ModelOnexSetupResult(BaseModel):
    """Every step that completed, and the failure that stopped the walk, if one did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path | None
    install_path: str | None
    steps: tuple[ModelOnexSetupStepOutcome, ...]
    failure: ModelOnexSetupFailure | None = None


__all__ = [
    "ModelOnexSetupFailure",
    "ModelOnexSetupRequest",
    "ModelOnexSetupResult",
    "ModelOnexSetupState",
    "ModelOnexSetupStepOutcome",
]
