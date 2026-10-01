# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A read of one contract-declared projection exposure (OMN-20159)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelProjectionReadRequest(BaseModel):
    """The ``GET /projection/{topic}`` query, as a typed command payload.

    ``tenant_id`` is the tenant the read is scoped to. A runtime that consumed
    a command envelope carrying a tenant scopes to that one instead, and a
    payload tenant that disagrees with it is refused (``tenant_conflict``).

    The row filter is named ``row_correlation_id``, not ``correlation_id``: the
    runtime's local ingress stamps a field named ``correlation_id`` with the
    request's own correlation id, which would silently filter every read by an
    id no row carries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1, description="The exposure's snapshot topic.")
    tenant_id: str | None = Field(default=None, min_length=1)
    limit: int | None = Field(default=None, ge=1)
    since: str | None = None
    order: str | None = Field(default=None, pattern="^(?i:asc|desc)$")
    order_by: str | None = None
    row_correlation_id: str | None = None


__all__ = ["ModelProjectionReadRequest"]
