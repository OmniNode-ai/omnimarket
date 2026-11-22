# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The batch ingest door's overlay: every deployment fact, bound and exact topic is a value here, none a default.

Loaded at start; a registry, topic list or bound that breaks an inequality below is refused at load, so a door never
runs on an overlay it could not honour. The tenant registry holds ids and slugs only; the JWKS holds public keys only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelTenantBinding,
)

NODE_NAME = "node_gateway_batch_ingest_effect"
OVERLAY_FILE_ENV = "GATEWAY_BATCH_INGEST_OVERLAY_FILE"

TENANT_PREFIX = "tenant-"
MAX_SLUG_CHARS = 32
KAFKA_MAX_TOPIC_CHARS = 249
CLIENT_HEADER_SET = frozenset(
    {
        "host",
        "content-type",
        "content-length",
        "transfer-encoding",
        "authorization",
        "user-agent",
        "accept",
        "accept-encoding",
        "connection",
        "idempotency-key",
    }
)
SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", re.ASCII)
_TOPIC_PATTERN = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)


class ModelOverlayTopic(BaseModel):
    """One exactly allowed canonical topic and the broker's limit for it, as read from an authorized source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    topic_max_message_bytes: int = Field(gt=0)


class ModelProxyHeader(BaseModel):
    """A proxy-added request header the door admits, never copies into a record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9-]+$")
    max_value_bytes: int = Field(gt=0)


class ModelClaimsOverlay(BaseModel):
    """The lab JWKS verifier: issuer, the one audience, the public keys and the accepted algorithms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: Literal["lab-jwks"]
    issuer: str = Field(min_length=1)
    audience: Literal["gateway-attach"]
    algorithms: tuple[str, ...] = Field(min_length=1)
    jwks_keys: tuple[dict[str, str], ...] = Field(min_length=1)
    max_lifetime_seconds: int = Field(gt=0)


class ModelLimitsOverlay(BaseModel):
    """Ingress, in-flight, topic and per-record bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_batch_records: int = Field(ge=1)
    max_body_bytes: int = Field(gt=0)
    chunk_headroom_bytes: int = Field(gt=0)
    max_header_count: int = Field(gt=0)
    max_header_bytes: int = Field(gt=0)
    max_authorization_bytes: int = Field(gt=0)
    max_json_depth: int = Field(gt=0)
    read_idle_timeout_s: float = Field(gt=0)
    total_upload_deadline_s: float = Field(gt=0)
    max_inflight_requests: int = Field(gt=0)
    max_inflight_bytes: int = Field(gt=0)
    max_topic_chars: int = Field(gt=0)
    max_wire_topic_chars: int = Field(gt=0)
    max_key_chars: int = Field(gt=0)
    max_value_chars: int = Field(gt=0)
    max_record_headers: int = Field(ge=0)
    max_header_name_chars: int = Field(gt=0)
    max_header_value_chars: int = Field(gt=0)
    max_egress_value_bytes: int = Field(gt=0)


class ModelKafkaOverlay(BaseModel):
    """The producer's request limit, the declared per-record overheads and the delivery bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    producer_max_request_bytes: int = Field(gt=0)
    message_timeout_ms: int = Field(gt=0)
    record_overhead_bytes: int = Field(ge=0)
    header_overhead_bytes: int = Field(ge=0)


class ModelBatchIngestOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: ModelClaimsOverlay
    tenants: tuple[ModelTenantBinding, ...] = Field(min_length=1)
    topics: tuple[ModelOverlayTopic, ...] = Field(min_length=1)
    proxy_headers: tuple[ModelProxyHeader, ...] = ()
    limits: ModelLimitsOverlay
    kafka: ModelKafkaOverlay

    @model_validator(mode="after")
    def _registry(self) -> Self:
        ids = [t.tenant_id for t in self.tenants]
        slugs = [t.tenant_slug for t in self.tenants]
        if len(set(ids)) != len(ids):
            raise ValueError("tenants repeat a tenant_id")
        if len(set(slugs)) != len(slugs):
            raise ValueError(
                "tenants repeat a tenant_slug (a retired slug is never reused)"
            )
        for tenant in self.tenants:
            if SLUG_PATTERN.fullmatch(tenant.tenant_slug) is None:
                raise ValueError("a tenant_slug breaks the slug grammar")
        return self

    @model_validator(mode="after")
    def _topics(self) -> Self:
        seen: set[str] = set()
        for entry in self.topics:
            if entry.topic.startswith(TENANT_PREFIX):
                raise ValueError(
                    "an overlay topic starts with 'tenant-'; the door adds the prefix itself"
                )
            if _TOPIC_PATTERN.fullmatch(entry.topic) is None:
                raise ValueError(
                    "an overlay topic carries characters outside [A-Za-z0-9._-]"
                )
            if len(entry.topic) > self.limits.max_topic_chars:
                raise ValueError("an overlay topic is longer than max_topic_chars")
            if entry.topic in seen:
                raise ValueError("overlay topics repeat a topic")
            seen.add(entry.topic)
        return self

    @model_validator(mode="after")
    def _inequalities(self) -> Self:
        lim = self.limits
        if lim.max_wire_topic_chars > KAFKA_MAX_TOPIC_CHARS:
            raise ValueError(
                "max_wire_topic_chars exceeds the Kafka topic limit of 249"
            )
        if (
            lim.max_topic_chars + len(TENANT_PREFIX) + MAX_SLUG_CHARS + 1
            > lim.max_wire_topic_chars
        ):
            raise ValueError(
                "max_topic_chars plus the longest tenant prefix exceeds max_wire_topic_chars"
            )
        if lim.max_inflight_bytes < lim.max_body_bytes + lim.chunk_headroom_bytes:
            raise ValueError(
                "max_inflight_bytes cannot hold one reservation (max_body_bytes + chunk_headroom_bytes)"
            )
        if lim.total_upload_deadline_s < lim.read_idle_timeout_s:
            raise ValueError(
                "total_upload_deadline_s is shorter than read_idle_timeout_s"
            )
        names = {p.name for p in self.proxy_headers}
        if len(names) != len(self.proxy_headers) or names & CLIENT_HEADER_SET:
            raise ValueError("proxy_headers repeat a name or one a client may send")
        if lim.max_header_count < len(CLIENT_HEADER_SET) + len(names):
            raise ValueError(
                "max_header_count is below the number of declared header names"
            )
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None, *, explicit: object | None = None
) -> ModelBatchIngestOverlay:
    """The overlay document, from an explicit value, ``GATEWAY_BATCH_INGEST_OVERLAY_FILE`` or the overlay roots."""
    return load_structured_overlay(
        NODE_NAME,
        ModelBatchIngestOverlay,
        environ,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )


__all__ = [
    "CLIENT_HEADER_SET",
    "KAFKA_MAX_TOPIC_CHARS",
    "MAX_SLUG_CHARS",
    "NODE_NAME",
    "OVERLAY_FILE_ENV",
    "SLUG_PATTERN",
    "TENANT_PREFIX",
    "ModelBatchIngestOverlay",
    "ModelClaimsOverlay",
    "ModelKafkaOverlay",
    "ModelLimitsOverlay",
    "ModelOverlayTopic",
    "ModelProxyHeader",
    "load_overlay",
]
