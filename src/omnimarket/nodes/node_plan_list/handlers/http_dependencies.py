# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared configuration and the Postgres binding of the plan mount."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.catalog_postgres_pool import CatalogPostgresPool
from omnimarket.models.model_catalog_http_route import (
    ModelCatalogDatabaseConfig,
    ModelCatalogHttpRoute,
)
from omnimarket.nodes.node_plan_list.handlers.repository_plan_catalog_postgres import (
    RepositoryPlanCatalogPostgres,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ID = "node_plan_list"


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract.yaml must be a mapping")
    return raw


def load_route() -> ModelCatalogHttpRoute:
    """The route this mount serves, read from the contract."""
    return ModelCatalogHttpRoute.model_validate(_contract()["http_route"])


def load_database_config() -> ModelCatalogDatabaseConfig:
    """Non-secret database settings from the contract's config block."""
    return ModelCatalogDatabaseConfig.model_validate(_contract()["config"]["database"])


def build_postgres_pool() -> CatalogPostgresPool:
    """The overlay-backed pool; nothing is resolved until the first read."""
    config = load_database_config()
    return CatalogPostgresPool(
        overlay_id=OVERLAY_ID,
        dsn_secret=config.dsn_secret,
        max_size=config.pool_max_size,
        query_timeout_seconds=config.query_timeout_seconds,
    )


def build_postgres_repository(
    pool: CatalogPostgresPool,
) -> RepositoryPlanCatalogPostgres:
    return RepositoryPlanCatalogPostgres(pool)


__all__: list[str] = [
    "build_postgres_pool",
    "build_postgres_repository",
    "load_database_config",
    "load_route",
]
