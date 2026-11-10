# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay model whose fields match the frozen lab broker ACL declaration file."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelBrokerAclGrant(BaseModel):
    """One desired ACL grant, validated by the driver's AclGrant rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: Literal["TOPIC", "GROUP", "CLUSTER", "TRANSACTIONAL_ID"]
    pattern: Literal["LITERAL", "PREFIXED"]
    name: str = Field(min_length=1)
    operation: Literal[
        "READ",
        "WRITE",
        "DESCRIBE",
        "DELETE",
        "IDEMPOTENT_WRITE",
        "CREATE",
        "DESCRIBE_CONFIGS",
    ]


class ModelBrokerAclPrincipal(BaseModel):
    """A named principal and its non-empty grant list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(pattern=r"^User:[A-Za-z0-9][A-Za-z0-9._-]*$")
    grants: tuple[ModelBrokerAclGrant, ...] = Field(min_length=1)


class ModelBrokerAclOverlay(BaseModel):
    """Connection cells and the principal grant blocks of one overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    broker_container: str = Field(min_length=1)
    superuser_source_container: str = Field(min_length=1)
    principals: tuple[ModelBrokerAclPrincipal, ...] = Field(min_length=1)
