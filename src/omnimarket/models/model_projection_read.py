# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection read's typed request and result, shared by every caller (OMN-20159).

node_projection_read_effect handles these, and node_local_dashboard_serve_effect
dispatches them to it; both import them from here so neither node reaches into
the other's model package.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ModelProjectionReadResult(BaseModel):
    """One read's outcome.

    ``response`` is the body the standalone API's ``GET /projection/{topic}``
    returns for the same request and ``http_status`` its status code, so a
    client moving from that route to this node reads the same document. The
    typed fields lift what a caller branches on. A refusal always names its
    ``error``; ``ok`` is never true with an error, and never false without one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    ok: bool
    error: str | None = None
    detail: str | None = None
    http_status: int = Field(ge=100, le=599)
    row_count: int = Field(default=0, ge=0)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    tenant: str | None = None
    next_cursor: str | None = None
    truncated: bool = False
    response: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ok_iff_no_error(self) -> ModelProjectionReadResult:
        if self.ok == (self.error is not None):
            raise ValueError("a read is ok exactly when it names no error")
        if not self.ok and self.rows:
            raise ValueError("a refused read carries no rows")
        return self


__all__ = ["ModelProjectionReadRequest", "ModelProjectionReadResult"]
