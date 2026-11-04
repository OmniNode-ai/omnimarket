# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire models of the three BYOK inference-credential HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelInferenceCredentialListItem(BaseModel):
    """One row of the list route: ref and metadata only, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    name: str
    provider: str
    created_at: datetime
    revoked_at: datetime | None = None


class ModelInferenceCredentialHttpAnswer(BaseModel):
    """What a route handler answers: a status code and a JSON-able body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int = Field(..., ge=100, le=599)
    body: Any


class ModelInferenceCredentialHttpCall(BaseModel):
    """One authenticated request, as the mount hands it to a route handler.

    The tenant is the edge-resolved caller; it is never read from the path or
    the body. Credential headers are not carried.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    path_params: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None
    body: Any = None
    body_unparseable: bool = False


class ModelInferenceCredentialHandlerRef(BaseModel):
    """The handler class a route contract binds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    module: str = Field(..., min_length=1)


class ModelInferenceCredentialRouteContract(BaseModel):
    """The ``http_route`` block of one node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(..., pattern=r"^(GET|POST|DELETE)$")
    path: str = Field(..., pattern=r"^/[A-Za-z0-9._~\-/{}]+$")
    handler: ModelInferenceCredentialHandlerRef
    input_schema: dict[str, Any] | None = None
    success_status: int = Field(..., ge=200, le=299)
    max_body_bytes: int = Field(default=65536, ge=1)
    max_path_param_length: int | None = Field(default=None, ge=1)


class ModelInferenceCredentialDeployConfig(BaseModel):
    """Non-secret deployment values, read from the contract ``config`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    infisical_addr: str = ""
    infisical_project_id: str = ""
    infisical_environment_slug: str = ""
    infisical_secret_path: str = ""
    kafka_bootstrap_servers: str = ""


__all__: list[str] = [
    "ModelInferenceCredentialDeployConfig",
    "ModelInferenceCredentialHandlerRef",
    "ModelInferenceCredentialHttpAnswer",
    "ModelInferenceCredentialHttpCall",
    "ModelInferenceCredentialListItem",
    "ModelInferenceCredentialRouteContract",
]
