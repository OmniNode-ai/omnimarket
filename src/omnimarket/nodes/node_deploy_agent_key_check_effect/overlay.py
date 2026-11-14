# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_deploy_agent_key_check_effect: the secret reference and the detail bound, none in code.

Found at ``<root>/node_deploy_agent_key_check_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the
load. ``agent_key_secret_env`` is the NAME of the environment variable the runtime fills from the secret store with the
signing key the deploy agent expects, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_deploy_agent_key_check_effect"


class ModelDeployAgentKeyCheckOverlay(BaseModel):
    """``agent_key_secret_env`` is the env var name the runtime fills with the deploy agent's expected signing key;
    ``detail_max_chars`` the longest detail kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_key_secret_env: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    detail_max_chars: StrictInt = Field(ge=1)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDeployAgentKeyCheckOverlay:
    return load_structured_overlay(NODE_NAME, ModelDeployAgentKeyCheckOverlay, environ)
