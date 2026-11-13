# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_command_effect: every bound of the trigger, none in code.

Found at ``<root>/node_branch_deploy_command_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_branch_deploy_command_effect"


class ModelBranchDeployCommandOverlay(BaseModel):
    """``trigger_argv_prefix`` is the argv prefix for the trigger; ``env_file`` the absolute path to the env file;
    ``timeout_seconds`` the child timeout; ``detail_max_chars`` the longest detail kept; ``refused_marker`` the stderr
    prefix for a refusal; ``build_source`` and ``scope`` the build and scope flags."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_argv_prefix: tuple[str, ...] = Field(min_length=1)
    env_file: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    detail_max_chars: int = Field(ge=1)
    refused_marker: str = Field(min_length=1)
    build_source: str = Field(min_length=1)
    scope: str = Field(min_length=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBranchDeployCommandOverlay:
    return load_structured_overlay(NODE_NAME, ModelBranchDeployCommandOverlay, environ)
