# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the stateless batch ingest door."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelVerifiedClaims(BaseModel):
    """What a verified token says; the tenant comes from here only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    tenant_slug: str
    subject: str
    issuer: str
    expires_at_epoch: int


class ModelTenantBinding(BaseModel):
    """One registry row: a tenant id bound to one slug for good."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    tenant_slug: str = Field(min_length=1)
    status: Literal["active", "retired"] = "active"


class ModelWireRecord(BaseModel):
    """One admitted record, byte-for-byte what is produced (stamps included)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    key: bytes | None
    value: bytes
    headers: tuple[tuple[str, bytes], ...]
    event_id: str
    kafka_record_size: int


class ModelPublishOutcome(BaseModel):
    """The outcome of one record's own delivery callback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["published", "publish-refused", "delivery-unconfirmed"]
    detail: str = ""


class ModelBatchIngestRequest(BaseModel):
    """One batch: verified claims plus the decoded JSON document (not yet validated)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    claims: ModelVerifiedClaims
    document: object


class ModelBatchIngestResponse(BaseModel):
    """The door's answer: an HTTP status and a JSON body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: dict[str, object]


__all__ = [
    "ModelBatchIngestRequest",
    "ModelBatchIngestResponse",
    "ModelPublishOutcome",
    "ModelTenantBinding",
    "ModelVerifiedClaims",
    "ModelWireRecord",
]
