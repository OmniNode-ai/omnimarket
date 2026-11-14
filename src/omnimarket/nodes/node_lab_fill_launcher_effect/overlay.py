# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_fill_launcher_effect: every setting of the fire's judgement, none in code.

Found at ``<root>/node_lab_fill_launcher_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the
load. The policy block is the compute node's own ``ModelLabFillFireOutcomePolicy``: every marker, pattern, class name,
exit code and template rides on it, so the launcher spells no setting of its own.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lab_fill.model_lab_fill_fire_outcome_policy import (
    ModelLabFillFireOutcomePolicy,
)

NODE_NAME = "node_lab_fill_launcher_effect"


class ModelLabFillLauncherState(BaseModel):
    """Where the fire's files live, relative to a directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    state_relpath: str = Field(min_length=1)
    results_dirname: str = Field(min_length=1)
    latest_filename: str = Field(min_length=1)
    snapshot_relpath: str = Field(min_length=1)
    snapshot_name_template: str = Field(min_length=1)
    date_format: str = Field(min_length=1)


class ModelLabFillLauncherAlert(BaseModel):
    """How the alert the judgement names is delivered: the library, the function and the shell that send it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lib_env: str = Field(min_length=1)
    lib_relpath: str = Field(min_length=1)
    function: str = Field(min_length=1)
    shell: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    unavailable_exit: int


class ModelLabFillLauncherOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelLabFillLauncherState
    alert: ModelLabFillLauncherAlert
    policy: ModelLabFillFireOutcomePolicy


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLabFillLauncherOverlay:
    return load_structured_overlay(NODE_NAME, ModelLabFillLauncherOverlay, environ)
