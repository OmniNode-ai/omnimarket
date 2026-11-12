# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scoped broker permission overlay and the rows rendered from it (OMN-23589).

The overlay document has the shape the broker permission apply tool reads as a declaration (lane, host, compose project,
broker and superuser containers, principals with grants), plus its own cells: scope, ticket, why, the ssh target the operator names and the operator's command
(its tool, its declaration path and the command's two placeholders). Every key is required; an unknown key is refused.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumAclResourceType(StrEnum):
    TOPIC = "TOPIC"
    GROUP = "GROUP"
    CLUSTER = "CLUSTER"


class EnumAclPattern(StrEnum):
    LITERAL = "LITERAL"
    PREFIXED = "PREFIXED"


class EnumAclOperation(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    DESCRIBE = "DESCRIBE"
    DELETE = "DELETE"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    CREATE = "CREATE"
    DESCRIBE_CONFIGS = "DESCRIBE_CONFIGS"


class EnumBrokerAclFindingCode(StrEnum):
    PRINCIPAL_INVALID = "principal_invalid"
    RESOURCE_NAME_INVALID = "resource_name_invalid"
    DELETE_NOT_ON_GROUP = "delete_not_on_group"
    CLUSTER_NOT_LITERAL = "cluster_not_literal"
    DUPLICATE_GRANT = "duplicate_grant"
    FORBIDDEN_SCM_GRANT = "forbidden_scm_grant"
    SCOPE_MISMATCH = "scope_mismatch"
    TOPIC_WITHOUT_ROW = "topic_without_row"
    NODE_WITHOUT_CONTRACT = "node_without_contract"
    NODE_PRINCIPAL_CONFLICT = "node_principal_conflict"


class ModelBrokerAclGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    resource_type: EnumAclResourceType
    pattern: EnumAclPattern
    name: str
    operation: EnumAclOperation


class ModelBrokerAclPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    principal: str
    role: str = Field(min_length=1)
    grants: tuple[ModelBrokerAclGrant, ...] = Field(min_length=1)


class ModelBrokerAclScopeOverlay(BaseModel):
    """One scoped file: the rows one change needs and nothing else."""

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
    principals: tuple[ModelBrokerAclPrincipal, ...] = Field(min_length=1)


class ModelBrokerAclRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    principal: str
    resource_type: EnumAclResourceType
    pattern: EnumAclPattern
    name: str
    operation: EnumAclOperation


class ModelBrokerAclFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: EnumBrokerAclFindingCode
    detail: str


class ModelBrokerAclRenderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str = Field(min_length=1)
    overlay: ModelBrokerAclScopeOverlay


class ModelBrokerAclRenderResult(BaseModel):
    """The desired rows, sorted, and the operator's two commands; refused rows render nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: str
    status: Literal["rendered", "refused"]
    rows: tuple[ModelBrokerAclRow, ...] = ()
    declared: int = Field(default=0, ge=0)
    findings: tuple[ModelBrokerAclFinding, ...] = ()
    operator_dry_run: str = ""
    operator_apply: str = ""
