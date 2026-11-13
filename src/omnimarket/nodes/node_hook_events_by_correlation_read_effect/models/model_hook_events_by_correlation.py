# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, answer and contract configuration of the by-correlation read.

The tenant key of this projection is the tenant SLUG (hook_events.tenant_id
holds the slug), where the sibling delegation routes key on the tenant UUID.
The two are not interchangeable and are not aligned here.
"""

from __future__ import annotations

from typing import Any, Literal

from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from pydantic import BaseModel, ConfigDict, Field


class ModelHookEventsReadRequest(BaseModel):
    """One read, after the caller tenant has been resolved by the edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_slug: str = Field(min_length=1, description="Resolved caller tenant slug.")
    correlation_id: str = Field(min_length=1)
    limit: int = Field(gt=0)
    include_payload: bool = False


class ModelHookEventRow(BaseModel):
    """One projected hook event, correlation metadata first.

    ``payload`` is ``None`` unless the caller opted in with
    ``include_payload=true``.
    """

    correlation_id: str | None
    event_id: str | None
    run_id: str | None
    event_type: str
    source: str
    tenant_id: str
    occurred_at: str | None
    captured_at: str | None
    payload: dict[str, Any] | None = None


class ModelHookEventsByCorrelation(BaseModel):
    """The typed answer to "is this correlation id in the cloud ledger?".

    ``data_state`` is one of ``found``, ``not_found`` (the projection exists and
    holds no such row for the tenant in ``tenant_scope``) or
    ``projection_absent`` (the projection table does not exist yet).
    ``tenant_scope`` is present on every state: it is the caller's own resolved
    slug, never a caller-supplied value, so an empty answer reads "no such row
    under YOUR tenant" and discloses nothing about any other tenant.
    """

    correlation_id: str
    projection: str
    data_state: Literal["found", "not_found", "projection_absent"]
    tenant_scope: str
    count: int
    rows: list[ModelHookEventRow]
    generated_at: str


class ModelHookEventsHttpResponse(BaseModel):
    """The handler's status and JSON body for the mount to render."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: dict[str, Any]


class ModelHookEventsHttpConfig(BaseModel):
    """The contract's ``http_route`` block: the route and its bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handler: ModelHandlerRef
    path: str = Field(min_length=1)
    methods: tuple[Literal["GET"], ...]
    projection: str = Field(min_length=1)
    max_correlation_id_length: int = Field(gt=0)
    default_limit: int = Field(gt=0)
    max_limit: int = Field(gt=0)
    unbound_identity_detail: str = Field(min_length=1)
    identity_unavailable_detail: str = Field(min_length=1)
    unauthorized_detail: str = Field(min_length=1)
    absent_credential_code: str = Field(min_length=1)
    invalid_credential_code: str = Field(min_length=1)
    credential_headers: tuple[str, ...]
    not_configured_detail: str = Field(min_length=1)
    unavailable_detail: str = Field(min_length=1)


__all__: list[str] = [
    "ModelHookEventRow",
    "ModelHookEventsByCorrelation",
    "ModelHookEventsHttpConfig",
    "ModelHookEventsHttpResponse",
    "ModelHookEventsReadRequest",
]
