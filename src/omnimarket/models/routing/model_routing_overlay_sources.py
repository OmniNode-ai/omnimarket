# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Database source declarations shared by routing's optional read boundaries."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingOverlaySource(BaseModel):
    """The environment secret reference a contract binds to one database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class ModelRoutingOverlaySources(BaseModel):
    """Keep projection and verdict database identities distinct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection: ModelRoutingOverlaySource
    verdict: ModelRoutingOverlaySource
