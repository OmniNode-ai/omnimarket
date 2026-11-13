# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_probe_effect: every bound of the probe, none in code.

The probe reads three HTTP routes: the runtime's health answer, the runtime's ready answer,
and the deploy agent's build provenance answer. Found at
``<root>/node_branch_deploy_probe_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic
values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_branch_deploy_probe_effect"


class ModelHttpProbeSettings(BaseModel):
    """The HTTP probe of the running runtime: its health and ready answers and the commit its build recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    health_url: str = Field(min_length=1)
    #: Keys from the health answer's root to the boolean that says the runtime serves.
    health_ok_path: tuple[str, ...] = Field(min_length=1)
    #: The runtime's ready answer: any 2xx says ready, anything else says not ready.
    ready_url: str = Field(min_length=1)
    #: The deploy agent's build provenance answer for the running build.
    provenance_url: str = Field(min_length=1)
    #: Keys from the provenance answer's root to the exact commit the running build was made from.
    sha_path: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelBranchDeployProbeOverlay(BaseModel):
    """``probe`` the HTTP probe's settings; ``detail_max_chars`` the longest detail kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: ModelHttpProbeSettings
    detail_max_chars: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBranchDeployProbeOverlay:
    return load_structured_overlay(NODE_NAME, ModelBranchDeployProbeOverlay, environ)
