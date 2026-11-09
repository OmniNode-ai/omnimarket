# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ONEX wire envelope the public example routes answer with.

The field set and names are the ones the public API published. Nothing here
holds an application name, a version or an example value: the builder supplies
every one from the route contract or its overlay.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field


class ModelEnvelopeMetadata(BaseModel):
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    request_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, Any] = Field(default_factory=dict)


class ModelOnexEnvelope(BaseModel):
    envelope_id: uuid.UUID
    envelope_version: str
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    source_node: str
    source_node_id: uuid.UUID | None = None
    target_node: str | None = None
    target_node_id: uuid.UUID | None = None
    handler_type: str | None = None
    operation: str
    timestamp: dt.datetime
    is_response: bool = False
    success: bool | None = None
    error: str | None = None
    error_details: dict[str, Any] | None = None
    metadata: ModelEnvelopeMetadata = Field(default_factory=ModelEnvelopeMetadata)
    payload: dict[str, Any] = Field(default_factory=dict)
    tenant_id: uuid.UUID | None = None
    tenant_slug: str | None = None
    node_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


__all__ = ["ModelEnvelopeMetadata", "ModelOnexEnvelope"]
