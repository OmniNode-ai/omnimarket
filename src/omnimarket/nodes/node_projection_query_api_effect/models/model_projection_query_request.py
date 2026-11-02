# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A request to the projection query node: one declared route and the inputs it reads (OMN-21288)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.projection.generation_publisher import ModelGenerateRequest


class ModelProjectionQueryRequest(BaseModel):
    """Which contract-declared route to answer, and its already-validated inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route_id: str
    topic: str | None = None
    correlation_id: str | None = None
    since: str | None = None
    limit: int | None = None
    order: str | None = None
    order_by: str | None = None
    tenant: str | None = None
    cursor: str | None = None
    ticket_id: str | None = None
    repo: str | None = None
    pr_number: int | None = None
    refresh: int | None = None
    generate: ModelGenerateRequest | None = None


__all__ = ["ModelProjectionQueryRequest"]
