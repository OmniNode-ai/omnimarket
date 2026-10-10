# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The shape of the packaged ``configs/routing_tiers.yaml`` file (OMN-20287).

The runtime DTO the ladder is parsed into is omnibase_core's
``ModelRoutingTier`` / ``ModelTierModel`` (through
``omnimarket.models.delegation.wire.parse_delegation_config_yaml``), which
renames ``backend_id`` to ``backend_ref`` and is owned by another repository.
The deployment-fact marker belongs to the file this package ships, so it is
declared here, on the file's own keys. Every block and key the file carries is
declared, so a new one is reported by the deployment-fact gate until it is
declared and, when it records a deployment's choice, marked.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind
from omnimarket.models.delegation.model_deployment_fact_marker import deployment_fact


class ModelPackagedTierModel(BaseModel):
    """One model a tier serves."""

    model_config = ConfigDict(frozen=True, extra="allow")

    id: str = Field(
        ...,
        min_length=1,
        description="The served model id.",
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.MODEL_NAME),
    )
    backend_id: str = Field(
        ...,
        min_length=1,
        description="The bifrost_delegation.yaml backend that serves it.",
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.BACKEND),
    )
    max_context_tokens: int | None = None
    use_for: tuple[str, ...] | str = ()
    fast_path_threshold_tokens: int | None = None


class ModelPackagedRoutingTier(BaseModel):
    """One rung of the escalation ladder."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str = Field(
        ...,
        min_length=1,
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.ROUTING_TIER),
    )
    cost_per_1k_tokens: float | None = None
    cost: dict[str, Any] | None = None
    models: tuple[ModelPackagedTierModel, ...] = ()
    eval_before_accept: bool = False
    eval_model: str | None = Field(
        default=None,
        description="The model a tier's answer is evaluated by.",
        json_schema_extra=deployment_fact(EnumDeploymentFactKind.MODEL_NAME),
    )
    max_retries: int | None = None


class ModelPackagedRoutingTiers(BaseModel):
    """The packaged ``routing_tiers.yaml``."""

    model_config = ConfigDict(frozen=True, extra="allow")

    tiers: tuple[ModelPackagedRoutingTier, ...] = Field(..., min_length=1)


__all__: list[str] = [
    "ModelPackagedRoutingTier",
    "ModelPackagedRoutingTiers",
    "ModelPackagedTierModel",
]
