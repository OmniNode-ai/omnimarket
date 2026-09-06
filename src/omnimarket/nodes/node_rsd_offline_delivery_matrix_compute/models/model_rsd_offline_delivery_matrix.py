# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and permanently non-authorizing output for offline C0."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.rsd.route_contract_view import ModelRsdRouteContractView


class ModelRsdOfflineDeliveryMatrixInput(BaseModel):
    """All provenance is supplied by value; the handler performs no resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    route_contract: ModelRsdRouteContractView


class ModelRsdOfflineDeliveryMatrixOutput(BaseModel):
    """Diagnostic result that is explicitly incapable of enabling dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    routing_tiers_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bifrost_delegation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    endpoint_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_registry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_contract_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tier_name: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    model_registry_key: str = Field(min_length=1)
    served_model: str = Field(min_length=1)
    endpoint_provider: str = Field(min_length=1)
    registry_provider_class: str = Field(min_length=1)
    route_max_context_tokens: int = Field(ge=1)
    endpoint_context_window: int = Field(ge=1)
    non_authorizing: Literal[True] = True
    effects_allowed: Literal[False] = False


__all__ = [
    "ModelRsdOfflineDeliveryMatrixInput",
    "ModelRsdOfflineDeliveryMatrixOutput",
]
