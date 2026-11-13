# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_command_effect: every bound of the bus publish, none in code.

Found at ``<root>/node_branch_deploy_command_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
``hmac_secret_env`` is the NAME of the environment variable the runtime fills from the secret store, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_branch_deploy_command_effect"


class ModelBranchDeployCommandOverlay(BaseModel):
    """``hmac_secret_env`` is the env var name the runtime fills with the deploy agent's signing key; ``scope`` and
    ``build_source`` the scope and build-source flags; ``detail_max_chars`` the longest detail kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hmac_secret_env: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    build_source: str = Field(min_length=1)
    detail_max_chars: int = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBranchDeployCommandOverlay:
    return load_structured_overlay(NODE_NAME, ModelBranchDeployCommandOverlay, environ)
