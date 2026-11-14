# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_command_effect: every bound of the bus publish, none in code.

Found at ``<root>/node_branch_deploy_command_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load —
except ``hmac_secret_env`` alone, tolerated absent during the transition to the overlays change (OMN-24085), because
overlays main still holds the old subprocess-trigger document. ``hmac_secret_env`` is the NAME of the environment
variable the runtime fills from the secret store, never a value. ``agent_health_url``, ``agent_health_timeout_seconds`` and
``agent_key_check_field`` (OMN-24396) are absent until the deploy agent serves a key check value: all three absent means
the handler does not check the signing key against the agent's key, all three present means it does, and a part of them
refuses.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_branch_deploy_command_effect"


class ModelBranchDeployCommandOverlay(BaseModel):
    """``hmac_secret_env`` is the env var name the runtime fills with the deploy agent's signing key (absent means the
    overlay does not name the signing key variable yet, tolerated only during the OMN-24085 transition); ``scope`` and
    ``build_source`` the scope and build-source flags; ``detail_max_chars`` the longest detail kept; ``agent_health_url``
    the deploy agent's health page, ``agent_health_timeout_seconds`` the bound of that one read and ``agent_key_check_field``
    the health field that carries the agent's key check value (all absent: no key check; all present: the key is checked
    before every publish; a part: the handler refuses to publish)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hmac_secret_env: str | None = Field(default=None, min_length=1)
    scope: str = Field(min_length=1)
    build_source: str = Field(min_length=1)
    detail_max_chars: int = Field(ge=1)
    agent_health_url: str | None = Field(default=None, min_length=1)
    agent_health_timeout_seconds: float | None = Field(default=None, gt=0)
    agent_key_check_field: str | None = Field(default=None, min_length=1)

    # These four keys belong to the retired subprocess trigger; they are read by nothing and are removed, with
    # hmac_secret_env made required again, in the change that follows the overlays change (OMN-24085 follow-up).
    trigger_argv_prefix: tuple[str, ...] | None = None
    env_file: str | None = None
    timeout_seconds: int | None = None
    refused_marker: str | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBranchDeployCommandOverlay:
    return load_structured_overlay(NODE_NAME, ModelBranchDeployCommandOverlay, environ)
