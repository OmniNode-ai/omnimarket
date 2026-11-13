# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract topics, declared principals and the rows derived from them (OMN-19998, issue L1-04).

The principals overlay names which broker login runs which nodes; a node's rows follow from the topics its contract
declares (event_bus.subscribe_topics and publish_topics). Every key is required; an unknown key is refused.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.broker_acl.model_broker_acl_overlay import (
    ModelBrokerAclFinding,
    ModelBrokerAclRow,
)

_PLACEHOLDER_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


class EnumTopicDirection(StrEnum):
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"


class ModelContractTopicUse(BaseModel):
    """One topic one node's contract declares, and whether the node publishes or subscribes to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    direction: EnumTopicDirection


class ModelNodePrincipal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    principal: str = Field(min_length=1)
    role: str = Field(min_length=1)
    nodes: tuple[str, ...] = Field(min_length=1)


class ModelNodeRole(BaseModel):
    """A set of nodes run under several logins at once (a unit on every lab host): each login holds the rows of each node."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    nodes: tuple[str, ...] = Field(min_length=1)
    principals: tuple[str, ...] = Field(min_length=1)
    placeholders: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _placeholder_shape(self) -> ModelNodeRole:
        for placeholder in self.placeholders:
            if not _PLACEHOLDER_NAME.fullmatch(placeholder):
                raise ValueError(
                    f"placeholder {placeholder!r} does not match ^[A-Z][A-Z0-9_]*$"
                )
        if len(set(self.placeholders)) != len(self.placeholders):
            repeated = next(
                p for p in self.placeholders if self.placeholders.count(p) > 1
            )
            raise ValueError(f"role {self.name!r} names placeholder {repeated!r} twice")
        if self.placeholders and len(self.principals) > 1:
            raise ValueError(
                f"role {self.name!r} names placeholders but holds {len(self.principals)} principals"
            )
        return self


class ModelBrokerAclPrincipalsOverlay(BaseModel):
    """The declared principals: which login runs which nodes, one login per node (principals) or one role of many logins (roles)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    why: str = Field(min_length=1)
    principals: tuple[ModelNodePrincipal, ...] = ()
    roles: tuple[ModelNodeRole, ...] = ()

    @model_validator(mode="after")
    def _names_something(self) -> ModelBrokerAclPrincipalsOverlay:
        if not self.principals and not self.roles:
            raise ValueError("an overlay declares at least one principal or role")
        seen: dict[str, str] = {}
        for role in self.roles:
            for placeholder in role.placeholders:
                if placeholder in seen:
                    raise ValueError(
                        f"placeholder {placeholder!r} is named by both {seen[placeholder]!r} and {role.name!r}"
                    )
                seen[placeholder] = role.name
        return self

    def placeholder_logins(self) -> dict[str, str]:
        """placeholder -> the role's single principal (empty when no role names one)."""
        return {
            placeholder: role.principals[0]
            for role in self.roles
            for placeholder in role.placeholders
        }


class ModelBrokerAclDeriveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str = Field(min_length=1)
    topic_uses: tuple[ModelContractTopicUse, ...]
    principals: ModelBrokerAclPrincipalsOverlay
    declared_rows: tuple[ModelBrokerAclRow, ...] = ()
    # Derive only this node's rows (the readiness check asks what one node needs). A node no principal runs refuses.
    only_node: str | None = Field(default=None, min_length=1)


class ModelBrokerAclDeriveResult(BaseModel):
    """Derived rows, sorted; a topic no row covers is listed and refuses the check."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str
    status: Literal["derived", "refused"]
    rows: tuple[ModelBrokerAclRow, ...] = ()
    contract_topics: int = Field(default=0, ge=0)
    derived_rows: int = Field(default=0, ge=0)
    uncovered_topics: tuple[str, ...] = ()
    findings: tuple[ModelBrokerAclFinding, ...] = ()
