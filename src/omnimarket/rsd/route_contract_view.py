# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed, lab-neutral views of an already-resolved routing contract.

This module deliberately contains no path, endpoint, environment, or runtime
resolver. Callers must carry the exact routing-contract bytes and the selected
tuple into the pure offline validator.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$"


class ModelRsdSelectedRoute(BaseModel):
    """The one route tuple selected by the routing authority."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tier_name: str = Field(min_length=1, max_length=128, pattern=_IDENTITY_PATTERN)
    backend_id: str = Field(min_length=1, max_length=256, pattern=_IDENTITY_PATTERN)
    model_registry_key: str = Field(
        min_length=1, max_length=256, pattern=_IDENTITY_PATTERN
    )


class ModelRsdRouteContractView(BaseModel):
    """A fully supplied, non-authorizing view of one resolved route contract."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    routing_tiers_yaml: bytes = Field(min_length=1, max_length=262_144)
    bifrost_delegation_yaml: bytes = Field(min_length=1, max_length=524_288)
    endpoint_registry_yaml: bytes = Field(min_length=1, max_length=262_144)
    model_registry_yaml: bytes = Field(min_length=1, max_length=262_144)
    selected_route: ModelRsdSelectedRoute


__all__ = [
    "ModelRsdRouteContractView",
    "ModelRsdSelectedRoute",
]
