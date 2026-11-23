# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input of node_install_request_acl_compute (OMN-24546).

The scope cells the operator names, the install request effect overlay (whose host rows name the logins each host allows and the login each host serves as), and the node's topic names. The handler derives the broker permission declaration from them: one principal per login, each writing only its own writer topic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallRequestEffectOverlay,
)
from omnimarket.nodes.node_install_request_effect.runtime_install_request import (
    ModelInstallRequestTopics,
)


class ModelInstallAclScopeCells(BaseModel):
    """The scope cells the operator names; the same cells the broker ACL overlay carries apart from its principals."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    why: str = Field(min_length=1)
    operator_ssh_target: str = Field(min_length=1)
    operator_command: str = Field(min_length=1)
    declaration_path: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    broker_container: str = Field(min_length=1)
    superuser_source_container: str = Field(min_length=1)


class ModelInstallAclRequest(BaseModel):
    """The input: the scope cells, the effect overlay and the node's topic names."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cells: ModelInstallAclScopeCells
    overlay: ModelInstallRequestEffectOverlay
    topics: ModelInstallRequestTopics
    group_prefix: str = Field(min_length=1)


__all__: list[str] = [
    "ModelInstallAclRequest",
    "ModelInstallAclScopeCells",
]
