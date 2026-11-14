# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_branch_deploy_command_effect: every bound of the bus publish, none in code.

Found at ``<root>/node_branch_deploy_command_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load —
except ``hmac_secret_env`` alone, tolerated absent during the transition to the overlays change (OMN-24085), because
overlays main still holds the old subprocess-trigger document. ``hmac_secret_env`` is the NAME of the environment
variable the runtime fills from the secret store, never a value. ``check_agent_key`` (OMN-24396) is absent until the deploy path is ready for the check: absent or false means
the handler does not check the signing key against the agent's key, true means it asks node_deploy_agent_key_check_effect
for the agent's key check value before every publish and refuses a command signed with another key.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_branch_deploy_command_effect"


class ModelBranchDeployCommandOverlay(BaseModel):
    """``hmac_secret_env`` is the env var name the runtime fills with the deploy agent's signing key (absent means the
    overlay does not name the signing key variable yet, tolerated only during the OMN-24085 transition); ``scope`` and
    ``build_source`` the scope and build-source flags; ``detail_max_chars`` the longest detail kept; ``check_agent_key``
    whether the signing key is checked against the deploy agent's key before every publish (absent or false: it is not)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hmac_secret_env: str | None = Field(default=None, min_length=1)
    scope: str = Field(min_length=1)
    build_source: str = Field(min_length=1)
    detail_max_chars: int = Field(ge=1)
    check_agent_key: StrictBool | None = None

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
