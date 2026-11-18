# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_probe_effect: every bound of the probe, none in code.

Found at ``<root>/node_branch_deploy_probe_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic
values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import ModelProbeSettings

NODE_NAME = "node_branch_deploy_probe_effect"


class ModelBranchDeployProbeOverlay(BaseModel):
    """``probe`` the reused probe source's settings; ``detail_max_chars`` the longest detail kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: ModelProbeSettings
    detail_max_chars: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBranchDeployProbeOverlay:
    return load_structured_overlay(NODE_NAME, ModelBranchDeployProbeOverlay, environ)
