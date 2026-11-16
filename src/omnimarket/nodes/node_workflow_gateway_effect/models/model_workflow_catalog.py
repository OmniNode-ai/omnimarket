# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Catalog of the workflows the gateway can dispatch, read from the overlay."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowWireSpec(BaseModel):
    """Per-workflow wire stamping flags, replacing the old module-level sets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_keys: tuple[str, ...] = Field(..., min_length=1)
    stamp_emitted_at: bool
    stamp_context_pack: bool
    canonical_tenant_id: bool
    stamp_tenant_principal: bool


class ModelWorkflowCatalogEntry(BaseModel):
    """One dispatchable workflow type and how its wire is stamped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_type: str = Field(..., min_length=1)
    contract_id: str = Field(..., min_length=1)
    command_topic: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    enabled: bool
    payload_schema: dict[str, Any]
    wire: ModelWorkflowWireSpec


class ModelWorkflowCatalog(BaseModel):
    """The full set of catalog entries the gateway serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflows: tuple[ModelWorkflowCatalogEntry, ...] = Field(..., min_length=1)


__all__: list[str] = [
    "ModelWorkflowCatalog",
    "ModelWorkflowCatalogEntry",
    "ModelWorkflowWireSpec",
]
