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
``signing_key_store`` (OMN-24611) names where the node's own entry resolves the signing key from when the
``hmac_secret_env`` variable is unset in its environment: the folder and key name in the store, and the store
addressing block. Names and addresses only, never a value. When the variable is set anyway, its value is trusted
only when the block's ``verify_env_key`` is false; when it is true the entry asks the store for the dev key and
uses the variable's value only when its one-way check value (``omnimarket.models.model_deploy_agent_key_check.
key_check_value``) equals the store key's check value — a mismatch is a typed refusal and nothing is published.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_branch_deploy_command_effect"


class ModelSigningKeyStoreOverlay(BaseModel):
    """Where the entry resolves the deploy agent's signing key when the variable ``hmac_secret_env`` names is unset
    (OMN-24611): a folder and key name in the secret store, and the store addressing. Names and addresses only, never
    a value. When the variable is set anyway, its value wins only under ``verify_env_key: false``; under ``true``
    the store is asked for the dev key and the variable's value is used only when its one-way check value
    (``omnimarket.models.model_deploy_agent_key_check.key_check_value``) equals the store key's check value, else
    the send refuses and nothing is published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The folder holding the key, absolute; taken relative to the store's root folder.
    folder: str = Field(pattern=r"^/")
    #: The NAME of the key in the store, never a value.
    key: str = Field(min_length=1)
    #: The store addressing block: provider, address and identity references.
    secret_store: ModelSecretStoreOverlay
    #: Whether a value found in the environment variable is checked against the store's dev key. Required: true
    #: checks the two one-way check values and refuses a mismatch; false keeps the old behaviour where the
    #: variable wins and the store is never asked.
    verify_env_key: StrictBool


class ModelBranchDeployCommandOverlay(BaseModel):
    """``hmac_secret_env`` is the env var name the runtime fills with the deploy agent's signing key (absent means the
    overlay does not name the signing key variable yet, tolerated only during the OMN-24085 transition); ``scope`` and
    ``build_source`` the scope and build-source flags; ``detail_max_chars`` the longest detail kept; ``check_agent_key``
    whether the signing key is checked against the deploy agent's key before every publish (absent or false: it is not);
    ``event_bus`` the bus a caller outside the lane's runtime dials to send the signed command (read by the node's own entry through
    ``resolve_dialable_broker``; the broker address stays in the lane declaration). Absent, the command can only be
    sent through the lane's runtime (OMN-19998). ``signing_key_store`` the store the node's own entry resolves the
    signing key from when the ``hmac_secret_env`` variable is unset (OMN-24611); absent, an unset variable refuses
    as before."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hmac_secret_env: str | None = Field(default=None, min_length=1)
    scope: str = Field(min_length=1)
    build_source: str = Field(min_length=1)
    detail_max_chars: int = Field(ge=1)
    check_agent_key: StrictBool | None = None
    event_bus: ModelNodeEventBus | None = None
    signing_key_store: ModelSigningKeyStoreOverlay | None = None

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
