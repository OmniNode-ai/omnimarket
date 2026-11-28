# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan-assign overlay: the identity database's DSN is found by reference, never held.

Found at ``<root>/node_tenant_plan_assign_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``; the
package ships ``overlay.example.yaml`` with synthetic values. Nothing has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.tenant_plan_postgres import (
    TenantPlanPostgres,
    TenantPlanStoreUnavailableError,
)

NODE_NAME = "node_tenant_plan_assign_effect"


class ModelPlanAssignDatabaseOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(min_length=1)
    secret_resolver_config_path: str = Field(min_length=1)
    max_pool_size: int = Field(ge=1)
    query_timeout_seconds: float = Field(gt=0)


class ModelPlanAssignOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    realms: tuple[str, ...]
    database: ModelPlanAssignDatabaseOverlay


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelPlanAssignOverlay:
    return load_structured_overlay(NODE_NAME, ModelPlanAssignOverlay, environ)


def build_store(overlay: ModelPlanAssignOverlay) -> TenantPlanPostgres:
    database = overlay.database

    async def dsn() -> str:
        from omnimarket.inference.secret_store_resolver import (
            secret_store_from_config_path,
        )

        store = secret_store_from_config_path(database.secret_resolver_config_path)
        value = await store.get_secret(database.dsn_secret)
        if not value:
            raise TenantPlanStoreUnavailableError("database secret has no value")
        return str(value)

    return TenantPlanPostgres(
        dsn_resolver=dsn,
        max_pool_size=database.max_pool_size,
        query_timeout_seconds=database.query_timeout_seconds,
    )


__all__ = [
    "NODE_NAME",
    "ModelPlanAssignDatabaseOverlay",
    "ModelPlanAssignOverlay",
    "build_store",
    "load_overlay",
]
