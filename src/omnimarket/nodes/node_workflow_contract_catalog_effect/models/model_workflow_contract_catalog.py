# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the workflow contract catalog route: catalog entries, route config, answers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Public workflow_type charset: a DNS-ish lowercase token, safe to embed in log
# lines, metric labels and URLs without escaping.
WORKFLOW_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$")
# Symbolic topic-constant names are SCREAMING_SNAKE_CASE identifiers resolved
# against the contract's declared command topics.
TOPIC_CONSTANT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# The runtime dispatches on the namespaced event_type "<namespace>.<event>"; an
# un-namespaced token matches no dispatcher, so the grammar requires a dot.
EVENT_TYPE_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]*[a-z0-9](\.[a-z][a-z0-9-]*[a-z0-9])+$"
)
# Contract ids: "name:major.minor.patch" or a dotted multi-segment name.
_CONTRACT_ID_PATTERN = re.compile(
    r"^(?:[a-z][a-z0-9_.]*:[0-9]+\.[0-9]+\.[0-9]+|[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)$"
)
_SQL_TOKEN_PATTERN = re.compile(
    r"(?:--|;|/\*|\*/|\b(?:select|insert|update|delete|drop|alter|union)\b)",
    re.IGNORECASE,
)


def _reject_sql_tokens(value: str, field: str) -> None:
    if _SQL_TOKEN_PATTERN.search(value):
        raise ValueError(f"{field} contains disallowed SQL-like tokens")


class ModelWorkflowContractEntry(BaseModel):
    """One catalog entry: a public workflow type and the contract that owns its topic.

    ``enabled: false`` fences the entry: it is fully validated like a servable
    one but is never advertised.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_type: str
    contract_id: str
    command_topic_constant: str
    event_type: str
    payload_schema: dict[str, Any]
    enabled: bool = True

    @field_validator("workflow_type")
    @classmethod
    def _workflow_type(cls, value: str) -> str:
        _reject_sql_tokens(value, "workflow_type")
        if not WORKFLOW_TYPE_PATTERN.match(value):
            raise ValueError(
                "workflow_type must be lowercase alphanumeric with hyphens, "
                "3-63 chars, start/end alphanumeric"
            )
        return value

    @field_validator("contract_id")
    @classmethod
    def _contract_id(cls, value: str) -> str:
        _reject_sql_tokens(value, "contract_id")
        if not _CONTRACT_ID_PATTERN.match(value):
            raise ValueError("contract_id must be 'name:major.minor.patch'")
        return value

    @field_validator("command_topic_constant")
    @classmethod
    def _command_topic_constant(cls, value: str) -> str:
        if not TOPIC_CONSTANT_PATTERN.match(value):
            raise ValueError("command_topic_constant must be SCREAMING_SNAKE_CASE")
        return value

    @field_validator("event_type")
    @classmethod
    def _event_type(cls, value: str) -> str:
        _reject_sql_tokens(value, "event_type")
        if not EVENT_TYPE_PATTERN.match(value):
            raise ValueError("event_type must be the namespaced '<namespace>.<event>'")
        return value

    @field_validator("payload_schema")
    @classmethod
    def _payload_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject any schema that would let arbitrary JSON through."""
        if value.get("type") != "object":
            raise ValueError("payload_schema.type must be 'object'")
        if value.get("additionalProperties", True) is not False:
            raise ValueError("payload_schema.additionalProperties must be false")
        properties = value.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ValueError("payload_schema.properties must be a non-empty object")
        return value


class ModelWorkflowContractDocument(BaseModel):
    """The overlay document that carries the catalog."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[ModelWorkflowContractEntry]


class ModelAdvertisedWorkflowContract(BaseModel):
    """What the route advertises for one servable workflow type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_type: str
    contract_id: str
    payload_schema: dict[str, Any]


class ModelCatalogHandlerRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    module: str


class ModelCatalogRefusal(BaseModel):
    """One refusal body the route may answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: int = Field(..., ge=400, le=599)
    detail: str = Field(..., min_length=1)
    code: str | None = None


class ModelCatalogHttpRoute(BaseModel):
    """The ``http_route`` block of the node contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handler: ModelCatalogHandlerRef
    path: str
    methods: tuple[str, ...]
    catalog_document: str = Field(..., min_length=1)
    credential_headers: tuple[str, ...]
    command_topics: Mapping[str, str]
    refusals: Mapping[str, ModelCatalogRefusal]

    @field_validator("refusals")
    @classmethod
    def _refusals_complete(
        cls, value: Mapping[str, ModelCatalogRefusal]
    ) -> Mapping[str, ModelCatalogRefusal]:
        required = {
            "credential_absent",
            "credential_invalid",
            "resolver_unbound",
            "catalog_unavailable",
        }
        missing = required - set(value)
        if missing:
            raise ValueError(f"http_route.refusals missing {sorted(missing)}")
        return value


class ModelCatalogRequest(BaseModel):
    """One catalog read: the request headers the identity resolver reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    headers: Mapping[str, str]


class ModelCatalogResponse(BaseModel):
    """The answer the HTTP adapter renders as-is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: int
    body: dict[str, Any]


__all__: list[str] = [
    "EVENT_TYPE_PATTERN",
    "TOPIC_CONSTANT_PATTERN",
    "WORKFLOW_TYPE_PATTERN",
    "ModelAdvertisedWorkflowContract",
    "ModelCatalogHandlerRef",
    "ModelCatalogHttpRoute",
    "ModelCatalogRefusal",
    "ModelCatalogRequest",
    "ModelCatalogResponse",
    "ModelWorkflowContractDocument",
    "ModelWorkflowContractEntry",
]
