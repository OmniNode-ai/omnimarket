# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic model for the setup journal state."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model_setup_legacy_hints import ModelSetupLegacyHints
from .model_setup_observation import ModelSetupObservation
from .model_setup_plan import ModelSetupPlan


class ModelSetupJournal(BaseModel):
    """The persisted setup journal state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    workspace: Path
    install_path: Literal["local", "docker"]
    desired_digest: str
    observations: tuple[ModelSetupObservation, ...] = ()
    imported_legacy: bool = False
    plan: ModelSetupPlan | None = None
    legacy_hints: ModelSetupLegacyHints | None = None
