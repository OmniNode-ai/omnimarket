# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Row derivation for the cloud-side hook-event ledger (OMN-17201, leg 5).

Every refusal in this module is fail-closed and named. There is no branch that
guesses, defaults, or silently drops: a record either derives a complete row or
raises :class:`HookLedgerProjectionError`, which the runner classifies as POISON
so the record lands durably in the DLQ instead of being retried forever or
committed-and-lost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_tenant_from_wire_topic,
)
from pydantic import BaseModel, ConfigDict, Field

#: Marks rows that arrived over the gateway relay, distinguishing them from the
#: rows ``node_hook_event_capture`` writes into the same table from the
#: workflow-submission spool path that OMN-16980 retires.
RELAY_SOURCE = "gateway-relay"

#: Keys the projection runner attaches to an unwrapped payload. They are the
#: runner's own bookkeeping, never producer data, and must not reach the stored
#: body.
_SYNTHETIC_KEY_PREFIX = "_"


class HookLedgerProjectionError(ValueError):
    """A record that can never project, no matter how often it is retried.

    Deliberately a ``ValueError`` subclass so
    ``omnimarket.projection.error_classification.classify_projection_error``
    resolves it as POISON: routed to the DLQ with the offset committed, rather
    than re-read in a hot loop behind a record that will never succeed. That
    distinction is not academic here -- OMN-17382 is the live record of one
    un-quarantinable record wedging a sibling leg for 7h45m across 925
    consecutive retries with 177 real records stuck behind it.
    """


class ModelHookLedgerInbound(BaseModel):
    """The producer-side hook body this ledger accepts.

    Extra fields are ALLOWED and preserved: the hook classes are independently
    versioned and new fields appear without a release of this node. The body is
    stored verbatim as JSONB, so narrowing it here would make this model a
    second, always-stale copy of contracts it does not own.
    """

    model_config = ConfigDict(extra="allow")

    emitted_at: datetime = Field(
        description="The producer's own timestamp. Never ingest time."
    )
    correlation_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)


def _canonical_body(payload: dict[str, Any]) -> str:
    """Stable JSON text of the event body, for content addressing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def derive_event_sha(canonical_topic: str, payload: dict[str, Any]) -> str:
    """Content address of one hook event.

    Deliberately derived from the event's OWN content and class only, never
    from the delivery coordinates. A consumer-group rebalance re-reads the same
    record at a different partition/offset; if those took part in the key, the
    table's ``UNIQUE (tenant_id, event_sha)`` would stop deduplicating exactly
    when it matters and a rebalance would double every row.
    """
    material = f"{canonical_topic}\n{_canonical_body(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def derive_batch_sha(wire_topic: str, partition: int, offset: int) -> str:
    """Content address of the DELIVERY unit this record arrived in.

    ``hook_events.batch_sha`` is NOT NULL because the spool path submits real
    multi-event batches. A bus record has no batch, so rather than reusing
    ``event_sha`` (which would assert a falsehood: that the batch and the event
    are the same thing) this addresses the genuine unit of delivery -- one
    broker record at one coordinate. It is honest, unique per delivery, and
    never confusable with a submitted batch's sha.
    """
    return hashlib.sha256(f"{wire_topic}:{partition}:{offset}".encode()).hexdigest()


def _require_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return the attached cloud envelope, or refuse.

    The cloud bus carries ``ModelEventEnvelope`` JSON -- the gateway forwarder
    decodes, validates and re-encodes one on every outbound publish. The
    STABILITY lane carries a FLAT hook body with its envelope metadata in Kafka
    headers instead. Those two shapes are not interchangeable, and conflating
    them silently is precisely the OMN-17919 defect one leg up the chain, where
    261 of 261 records were rejected while the unit suite stayed green because
    every fixture built the shape the live lane does not carry.

    So this refuses by name rather than falling back to treating a flat record
    as its own payload.
    """
    envelope = data.get("_envelope")
    if not isinstance(envelope, dict):
        raise HookLedgerProjectionError(
            "hook ledger record carries no cloud envelope: the cloud bus wire "
            "shape is ModelEventEnvelope, and a flat body is the stability-lane "
            "shape (OMN-17919). Refusing rather than guessing which one this is."
        )
    return envelope


def _resolve_tenant(wire_topic: str, envelope: dict[str, Any]) -> tuple[str, str]:
    """Resolve ``(tenant_slug, canonical_topic)`` from the WIRE TOPIC, cross-checked.

    Two independent refusals, both fail-closed:

    * The topic must actually carry a ``tenant-<slug>.`` prefix. A bare topic
      leaves the tenant underived, and a payload-supplied ``tenant_id`` would
      then survive unverified -- the cross-tenant identity leak the
      ``tenant_scoped_ingress`` gate exists to prevent.
    * When the forwarder's own trust-boundary tag is present it must AGREE. Two
      tenant authorities that disagree is a refusal, never a pick. OMN-17066 is
      the live record of the alternative: a writer keying tenant isolation on a
      single house-tenant stamp collapsed cross-tenant events onto one
      idempotency key.

    The payload's own ``tenant_id`` is never an input to this. It is producer-
    supplied and therefore not an authority on tenancy at all.
    """
    slug, canonical_topic = resolve_tenant_from_wire_topic(wire_topic)
    if slug is None:
        raise HookLedgerProjectionError(
            f"hook ledger wire topic carries no tenant prefix: {wire_topic!r}. "
            "The row tenant is derived from the wire topic and there is no "
            "second source to fall back to."
        )

    tags = envelope.get("metadata")
    tag_slug: object = None
    if isinstance(tags, dict):
        raw_tags = tags.get("tags")
        if isinstance(raw_tags, dict):
            tag_slug = raw_tags.get("gateway_tenant_slug")
    if tag_slug is not None and tag_slug != slug:
        raise HookLedgerProjectionError(
            "hook ledger tenant authorities disagree: wire topic says "
            f"{slug!r} and the gateway tenant tag says {tag_slug!r}. Refusing."
        )
    return slug, canonical_topic


def _reject_payload_tenant_claim(payload: dict[str, Any], tenant_id: str) -> None:
    """A producer-supplied tenant that contradicts the wire topic is refused."""
    claimed = payload.get("tenant_id")
    if claimed is not None and claimed != tenant_id:
        raise HookLedgerProjectionError(
            "hook ledger payload claims tenant "
            f"{claimed!r} but the wire topic resolves tenant {tenant_id!r}. "
            "The payload is producer-supplied and is not an authority on "
            "tenancy; refusing rather than trusting either one."
        )


def _require_occurred_at(payload: dict[str, Any]) -> datetime:
    """The producer's own timestamp, or a refusal. Never ``now()``.

    ``hook_events.occurred_at``'s own migration is explicit that ingest time
    must never be stamped here, because these events are historical and their
    producer timestamp is the only ordering signal they carry. A record without
    one is refused rather than given a fabricated position in the ledger.
    """
    raw = payload.get("emitted_at")
    if raw is None:
        raise HookLedgerProjectionError(
            "hook ledger record carries no emitted_at. occurred_at is the "
            "producer's own timestamp and is never backfilled with ingest "
            "time; refusing rather than fabricating an ordering position."
        )
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as err:
        raise HookLedgerProjectionError(
            f"hook ledger emitted_at is not an ISO-8601 timestamp: {raw!r}"
        ) from err


def _stored_payload(data: dict[str, Any]) -> dict[str, Any]:
    """The verbatim producer body, with the runner's own bookkeeping removed."""
    return {k: v for k, v in data.items() if not k.startswith(_SYNTHETIC_KEY_PREFIX)}


def _optional_str(value: object, *, limit: int) -> str | None:
    """A bounded string, or ``None``.

    The columns these feed are ``VARCHAR(64)``. An over-long value is truncated
    to ``None`` rather than to a prefix: a truncated correlation id reads like a
    real one and would silently fail to match the id the AC3 probe searches for,
    which is worse than an honest absence.
    """
    if value is None:
        return None
    text = str(value)
    if not text or len(text) > limit:
        return None
    return text


def derive_hook_ledger_row(
    *,
    wire_topic: str,
    data: dict[str, Any],
    partition: int,
    offset: int,
) -> dict[str, Any]:
    """Derive exactly one ``public.hook_events`` row from one cloud bus record."""
    envelope = _require_envelope(data)
    tenant_id, canonical_topic = _resolve_tenant(wire_topic, envelope)

    payload = _stored_payload(data)
    _reject_payload_tenant_claim(payload, tenant_id)
    occurred_at = _require_occurred_at(payload)

    correlation_id = _optional_str(
        payload.get("correlation_id") or envelope.get("correlation_id"), limit=64
    )

    return {
        "tenant_id": tenant_id,
        "event_sha": derive_event_sha(canonical_topic, payload),
        "event_type": canonical_topic,
        "occurred_at": occurred_at,
        "payload": payload,
        "event_id": _optional_str(envelope.get("envelope_id"), limit=64),
        "correlation_id": correlation_id,
        "run_id": _optional_str(payload.get("session_id"), limit=64),
        "source": RELAY_SOURCE,
        "batch_sha": derive_batch_sha(wire_topic, partition, offset),
    }
