# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one HTTP route a catalog node's contract declares for its runtime mount."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCatalogHttpHandlerBinding(BaseModel):
    """The handler class a route is bound to, by module and name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    module: str = Field(..., min_length=1)


class ModelCatalogHttpRoute(BaseModel):
    """Method, path, input schema and handler binding of one contract-declared route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET"]
    path: str = Field(..., pattern=r"^/[A-Za-z0-9._~/\-]+$")
    contract_id: str = Field(..., min_length=1)
    authorization: Literal["anonymous", "tenant"]
    input_schema: dict[str, Any]
    handler: ModelCatalogHttpHandlerBinding


class ModelCatalogDatabaseConfig(BaseModel):
    """Non-secret database settings a catalog node's contract carries in its config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(..., min_length=1, description="Logical secret name.")
    pool_max_size: int = Field(..., ge=1)
    query_timeout_seconds: float = Field(..., gt=0)


__all__: list[str] = [
    "ModelCatalogDatabaseConfig",
    "ModelCatalogHttpHandlerBinding",
    "ModelCatalogHttpRoute",
]
