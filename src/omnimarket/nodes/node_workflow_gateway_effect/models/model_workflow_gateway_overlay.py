# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment document ``node_workflow_gateway_effect/overlay.yaml``."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowCatalog,
)


class ModelWorkflowGatewayDatabase(BaseModel):
    """How the gateway reaches its Postgres store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_resolver_config_path: str = Field(..., min_length=1)
    dsn_secret: str = Field(..., min_length=1, description="Logical secret name.")
    pool_max_size: int = Field(..., gt=0)
    query_timeout_seconds: float = Field(..., gt=0)


class ModelWorkflowGatewayApiKey(BaseModel):
    """How the gateway authenticates callers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    header: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)


class ModelWorkflowGatewayRateLimit(BaseModel):
    """The token-bucket shape of the per-principal rate limiter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refill_per_minute: int = Field(..., ge=1)
    burst_capacity: int = Field(..., ge=1)


class ModelWorkflowGatewayQuota(BaseModel):
    """The entitlement keys the monthly quota and the per-tenant rate limit are read under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_key: str = Field(..., min_length=1)
    window_key: str = Field(..., min_length=1)
    lock_namespace: str = Field(..., min_length=1)
    rpm_metric_key: str = Field(..., min_length=1)
    rpm_window_key: str = Field(..., min_length=1)


class ModelWorkflowGatewayOverlay(BaseModel):
    """``node_workflow_gateway_effect/overlay.yaml``: every field is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ModelEdgeIdentityOverlay
    database: ModelWorkflowGatewayDatabase
    api_key: ModelWorkflowGatewayApiKey
    rate_limit: ModelWorkflowGatewayRateLimit
    quota: ModelWorkflowGatewayQuota
    catalog: ModelWorkflowCatalog
    gateway_instance_id: str = Field(..., min_length=1)
    envelope_schema_version: str = Field(..., min_length=1)
    publish_timeout_seconds: float = Field(..., gt=0)

    @field_validator("envelope_schema_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
            raise ValueError("envelope_schema_version must be a semver string")
        return value


__all__: list[str] = [
    "ModelWorkflowGatewayApiKey",
    "ModelWorkflowGatewayDatabase",
    "ModelWorkflowGatewayOverlay",
    "ModelWorkflowGatewayQuota",
    "ModelWorkflowGatewayRateLimit",
]
