# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The route a tenant-read node declares in its contract, read by its mount factory."""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from pydantic import BaseModel, ConfigDict, Field


class ModelTenantReadLimit(BaseModel):
    """The bounded ``limit`` query parameter of a list read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default: int = Field(ge=1)
    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)


class ModelTenantReadRoute(BaseModel):
    """Method, path and handler binding of one tenant-scoped GET."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handler: ModelHandlerRef
    path: str = Field(pattern=r"^/.+[^/]$")
    methods: tuple[Literal["GET"], ...]
    limit: ModelTenantReadLimit | None = None


__all__: list[str] = ["ModelTenantReadLimit", "ModelTenantReadRoute"]
