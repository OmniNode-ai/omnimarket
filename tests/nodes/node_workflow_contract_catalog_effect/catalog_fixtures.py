# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seed data and fakes for the workflow contract catalog mount tests."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelAdvertisedWorkflowContract,
)

TENANT_A_KEY = "fixture-tenant-a-key"
PROMPT_CEILING = 65536
DOGFOOD_STANDUP_PROMPT_CHARS = 58_368
DELEGATION = "delegation-inference"
FENCED = "push-validation"

DELEGATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["prompt", "task_type"],
    "additionalProperties": False,
    "properties": {
        "prompt": {"type": "string", "minLength": 1, "maxLength": PROMPT_CEILING},
        "task_type": {
            "type": "string",
            "pattern": "^(test|document|research|code_generation|review)$",
        },
        "max_tokens": {"type": "integer", "minimum": 1},
        "context_pack": {"type": "string", "maxLength": PROMPT_CEILING},
    },
}


def _entry(
    workflow_type: str,
    *,
    constant: str = "DELEGATION_REQUEST_TOPIC",
    schema: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "workflow_type": workflow_type,
        "contract_id": "node_delegation_orchestrator:0.6.0",
        "command_topic_constant": constant,
        "event_type": "omnibase-infra.delegation-request",
        "payload_schema": copy.deepcopy(schema or DELEGATION_SCHEMA),
    }
    if enabled is not None:
        entry["enabled"] = enabled
    return entry


def catalog_document() -> dict[str, Any]:
    """A fresh catalog: two servable types (out of order) and one fenced."""
    return {
        "workflows": [
            _entry(DELEGATION),
            _entry(
                FENCED,
                constant="PUSH_VALIDATION_REQUEST_TOPIC",
                enabled=False,
            ),
            _entry("alpha-flow"),
        ]
    }


def entry(workflow_type: str, **kwargs: Any) -> dict[str, Any]:
    return _entry(workflow_type, **kwargs)


class SeededIdentity:
    """Resolves the seeded tenant key and nothing else."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.calls += 1
        if headers.get("x-api-key") != TENANT_A_KEY:
            return None
        return ModelHttpTenantIdentity(
            tenant_id=UUID("00000000-0000-0000-0000-00000000000a"),
            tenant_slug="tenant-a",
            principal_id="principal-a",
        )


class CountingCatalog:
    """A catalog source that records every read."""

    def __init__(self, contracts: tuple[ModelAdvertisedWorkflowContract, ...]) -> None:
        self._contracts = contracts
        self.reads = 0

    async def servable_contracts(self) -> tuple[ModelAdvertisedWorkflowContract, ...]:
        self.reads += 1
        return self._contracts


class InlineDocuments:
    """In-memory overlay documents (the in-memory profile's document binding)."""

    def __init__(self, documents: Mapping[str, dict[str, Any]]) -> None:
        self._documents = dict(documents)

    def read(self, name: str) -> dict[str, Any] | None:
        document = self._documents.get(name)
        return None if document is None else copy.deepcopy(document)
