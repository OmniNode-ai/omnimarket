# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs, HTTP answers and overlay configuration for the webhook door."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from pydantic import BaseModel, ConfigDict, Field


class ModelWebhookTarget(BaseModel):
    """Routing provenance supplied by the deployment, never by the request."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: UUID
    tenant_slug: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)


class ModelWebhookHttpConfig(BaseModel):
    """The contract's HTTP binding and names of overlay values."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    handler: ModelHandlerRef
    path: str
    methods: tuple[Literal["POST"], ...]
    delivery_topic: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    max_body_bytes: int = Field(gt=0)
    publish_timeout_seconds: float = Field(gt=0)
    environment: dict[str, str]


class ModelWebhookHttpRequest(BaseModel):
    """Unparsed request bytes and delivery headers; sensitive fields stay out of repr."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event: str = ""
    delivery_id: str = ""
    signature: str = Field(default="", repr=False)
    body: bytes = Field(default=b"", repr=False)


class ModelWebhookHttpResponse(BaseModel):
    """The original webhook route's status and JSON response."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status_code: int
    body: dict[str, str | bool]
