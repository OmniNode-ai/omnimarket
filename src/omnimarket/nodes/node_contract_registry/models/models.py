# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for contract registration request and result."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_contract_registry.models.enums import (
    EnumMaterializationRejection,
    EnumMaterializationStatus,
)


class ModelContractRegistrationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    contract_yaml: str
    contract_hash: str
    node_version: dict[str, int] = Field(default_factory=dict)
    correlation_id: UUID
    deployer_id: str = ""
    target_profile: str = ""


class ModelContractRegistrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    contract_hash: str
    correlation_id: UUID
    status: EnumMaterializationStatus
    event_type: str = "registered"
    contract_yaml: str = ""
    node_version: dict[str, int] = Field(default_factory=dict)
    deployer_id: str = ""
    target_profile: str = ""
    reason: EnumMaterializationRejection | None = None
    stored: bool = False
    published_topic: str = ""
    mcp_eligible: bool = False
    mcp_tags: tuple[str, ...] = Field(default_factory=tuple)


__all__ = ["ModelContractRegistrationRequest", "ModelContractRegistrationResult"]
