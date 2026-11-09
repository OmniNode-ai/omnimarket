# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model of a lab broker access declaration held in the overlay home (OMN-21611).

A declaration names the lane's broker, each principal's login and the ACL rows it holds, in the
format the broker access reconcile tool loads. The overlay home validates the document through this
model before holding it, so a malformed row is refused at the pull request and not at the operator's
apply. Imports pydantic alone, because that check installs pydantic and pyyaml
and nothing else. No password or key lives in a declaration: only the login names and their rows.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

BrokerAclOperation = Literal[
    "READ",
    "WRITE",
    "DESCRIBE",
    "DELETE",
    "IDEMPOTENT_WRITE",
    "CREATE",
    "DESCRIBE_CONFIGS",
]


class ModelBrokerAclGrant(BaseModel):
    """One ALLOW row for all hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: Literal["TOPIC", "GROUP", "CLUSTER"]
    pattern: Literal["LITERAL", "PREFIXED"]
    name: str = Field(min_length=1)
    operation: BrokerAclOperation

    @model_validator(mode="after")
    def _row_is_one_the_tool_accepts(self) -> Self:
        if self.name == "*":
            raise ValueError("a wildcard resource name is refused")
        if self.operation == "DELETE" and self.resource_type != "GROUP":
            raise ValueError(f"DELETE is allowed on GROUP only: {self.name!r}")
        if self.resource_type == "CLUSTER" and (
            self.name != "kafka-cluster" or self.pattern != "LITERAL"
        ):
            raise ValueError("CLUSTER rows must be LITERAL kafka-cluster rows")
        return self


class ModelBrokerAclPrincipal(BaseModel):
    """One login and the rows it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(pattern=r"^User:[A-Za-z0-9._-]+$")
    role: str = Field(min_length=1)
    grants: tuple[ModelBrokerAclGrant, ...] = Field(min_length=1)


class ModelBrokerAclDeclarationOverlay(BaseModel):
    """A lane's declared broker logins, in the format the reconcile tool loads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    broker_container: str = Field(min_length=1)
    superuser_source_container: str = Field(min_length=1)
    principals: tuple[ModelBrokerAclPrincipal, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_login_or_row_repeats(self) -> Self:
        names = [entry.principal for entry in self.principals]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"principals repeat: {repeated}")
        rows = [
            (entry.principal, grant)
            for entry in self.principals
            for grant in entry.grants
        ]
        if len(set(rows)) != len(rows):
            raise ValueError("a grant row repeats for one principal")
        return self
