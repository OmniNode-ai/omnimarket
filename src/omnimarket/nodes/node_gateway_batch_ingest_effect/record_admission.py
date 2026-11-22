# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whole-batch validation: phase A checks every record's shape and topic, phase B its egress gate and Kafka size.

Nothing here publishes. The first refusal in phase A order (every record), then phase B order (every record), is the
one the door answers with, so a bad last record is found before a middle record's size is looked at.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from omnimarket.nodes.node_gateway_batch_ingest_effect.event_identity import (
    derive_event_id,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelVerifiedClaims,
    ModelWireRecord,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.topic_rules import (
    TopicRefusedError,
    normalize_topic,
    wire_topic,
)

RECORD_FIELDS = frozenset(
    {"topic", "key", "value", "event_id", "envelope_id", "headers"}
)
# `message-id` is the shared contract's identity header: a client may send it, and the door replaces it with its stamp.
STAMP_HEADER_NAMES = frozenset(
    {"onex-tenant-id", "onex-tenant-slug", "onex-ingest-door"}
)
DOOR_NAME = b"gateway-batch-ingest"
_EVENT_ID = re.compile(r"[0-9a-f]{64}", re.ASCII)


class AdmissionRefusedError(Exception):
    """A batch or one of its records was refused; `index` is None for a refusal of the batch itself."""

    def __init__(self, reason: str, index: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.index = index


@dataclass(frozen=True)
class DecodedRecord:
    """A record that passed phase A."""

    canonical_topic: str
    key: bytes | None
    value: bytes
    event_id: str
    headers: tuple[tuple[str, bytes], ...]
    duplicate_members: bool


def batch_records(document: object, *, max_batch_records: int) -> list[object]:
    """The records list of a batch document; the document may carry nothing else, a tenant least of all."""
    if not isinstance(document, dict):
        raise AdmissionRefusedError("body-not-object")
    if set(document) - {"records"}:
        raise AdmissionRefusedError("unknown-field")
    records = document.get("records")
    if not isinstance(records, list):
        raise AdmissionRefusedError("records-missing")
    if not records:
        raise AdmissionRefusedError("batch-empty")
    if len(records) > max_batch_records:
        raise AdmissionRefusedError("batch-too-large")
    return records


def _decode_b64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise AdmissionRefusedError("record-base64-invalid") from None


def validate_shape(
    raw: object,
    claims: ModelVerifiedClaims,
    overlay: ModelBatchIngestOverlay,
) -> DecodedRecord:
    """Phase A for one record: fields, topic, length prechecks, then base64 and headers."""
    lim = overlay.limits
    if not isinstance(raw, dict):
        raise AdmissionRefusedError("record-not-object")
    if set(raw) - RECORD_FIELDS:
        raise AdmissionRefusedError("unknown-field")
    try:
        canonical = normalize_topic(
            raw.get("topic"),
            slug=claims.tenant_slug,
            max_topic_chars=lim.max_topic_chars,
            max_wire_topic_chars=lim.max_wire_topic_chars,
            allowed={entry.topic for entry in overlay.topics},
        )
    except TopicRefusedError as exc:
        raise AdmissionRefusedError(exc.reason) from None
    raw_key, raw_value = raw.get("key"), raw.get("value")
    if raw_key is not None and not isinstance(raw_key, str):
        raise AdmissionRefusedError("record-key-invalid")
    if not isinstance(raw_value, str):
        raise AdmissionRefusedError("record-value-invalid")
    # Length prechecks come first: base64 is never decoded on an over-length string.
    if (raw_key is not None and len(raw_key) > lim.max_key_chars) or len(
        raw_value
    ) > lim.max_value_chars:
        raise AdmissionRefusedError("record-too-large")
    raw_headers = raw.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise AdmissionRefusedError("record-headers-invalid")
    if len(raw_headers) > lim.max_record_headers:
        raise AdmissionRefusedError("record-headers-too-large")
    headers: list[tuple[str, bytes]] = []
    for name, text in raw_headers.items():
        if not isinstance(name, str) or not isinstance(text, str):
            raise AdmissionRefusedError("record-headers-invalid")
        if (
            len(name) > lim.max_header_name_chars
            or len(text) > lim.max_header_value_chars
        ):
            raise AdmissionRefusedError("record-headers-too-large")
        headers.append((name.lower(), text.encode("utf-8")))
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise AdmissionRefusedError("event-id-invalid")
    envelope_id = raw.get("envelope_id")
    if not isinstance(envelope_id, str) or not envelope_id.strip():
        raise AdmissionRefusedError("envelope-id-invalid")
    key = _decode_b64(raw_key) if raw_key is not None else None
    value = _decode_b64(raw_value)
    duplicated = bool(getattr(raw, "duplicates", ())) or bool(
        getattr(raw_headers, "duplicates", ())
    )
    return DecodedRecord(canonical, key, value, event_id, tuple(headers), duplicated)


def effective_limit(overlay: ModelBatchIngestOverlay, canonical_topic: str) -> int:
    """The smaller of the producer's request limit and the topic's own, both from the overlay."""
    topic_limit = next(
        e.topic_max_message_bytes for e in overlay.topics if e.topic == canonical_topic
    )
    return min(overlay.kafka.producer_max_request_bytes, topic_limit)


def kafka_record_size(
    overlay: ModelBatchIngestOverlay,
    key: bytes | None,
    value: bytes,
    headers: tuple[tuple[str, bytes], ...],
) -> int:
    """Record size with the stamps and the declared overheads counted."""
    kafka = overlay.kafka
    return (
        kafka.record_overhead_bytes
        + (len(key) if key is not None else 0)
        + len(value)
        + sum(
            len(n.encode("utf-8")) + len(v) + kafka.header_overhead_bytes
            for n, v in headers
        )
    )


def admit_record(
    decoded: DecodedRecord,
    claims: ModelVerifiedClaims,
    overlay: ModelBatchIngestOverlay,
) -> ModelWireRecord:
    """Phase B for one record: the egress gate, the identity check, the stamps and the size admission."""
    if decoded.duplicate_members:
        raise AdmissionRefusedError("egress-duplicate-member-name")
    names = [name for name, _ in decoded.headers]
    if len(set(names)) != len(names):
        raise AdmissionRefusedError("egress-duplicate-member-name")
    if len(decoded.value) > overlay.limits.max_egress_value_bytes:
        raise AdmissionRefusedError("egress-value-too-large")
    if STAMP_HEADER_NAMES.intersection(names):
        raise AdmissionRefusedError("egress-client-stamp-header")
    expected = derive_event_id(
        decoded.canonical_topic, decoded.key, decoded.headers, decoded.value
    )
    if expected != decoded.event_id:
        raise AdmissionRefusedError("event-id-mismatch")
    stamped = (
        *(pair for pair in decoded.headers if pair[0] != "message-id"),
        ("message-id", decoded.event_id.encode("ascii")),
        ("onex-tenant-id", claims.tenant_id.encode("utf-8")),
        ("onex-tenant-slug", claims.tenant_slug.encode("utf-8")),
        ("onex-ingest-door", DOOR_NAME),
    )
    size = kafka_record_size(overlay, decoded.key, decoded.value, stamped)
    if size > effective_limit(overlay, decoded.canonical_topic):
        raise AdmissionRefusedError("record-exceeds-kafka-limit")
    return ModelWireRecord(
        topic=wire_topic(claims.tenant_slug, decoded.canonical_topic),
        key=decoded.key,
        value=decoded.value,
        headers=stamped,
        event_id=decoded.event_id,
        kafka_record_size=size,
    )


def admit_batch(
    document: object,
    claims: ModelVerifiedClaims,
    overlay: ModelBatchIngestOverlay,
) -> list[ModelWireRecord]:
    """Validate the whole batch (phase A over every record, then phase B over every record); publish nothing."""
    records = batch_records(
        document, max_batch_records=overlay.limits.max_batch_records
    )
    decoded: list[DecodedRecord] = []
    for index, raw in enumerate(records):
        try:
            decoded.append(validate_shape(raw, claims, overlay))
        except AdmissionRefusedError as exc:
            raise AdmissionRefusedError(exc.reason, index) from None
    admitted: list[ModelWireRecord] = []
    for index, item in enumerate(decoded):
        try:
            admitted.append(admit_record(item, claims, overlay))
        except AdmissionRefusedError as exc:
            raise AdmissionRefusedError(exc.reason, index) from None
    return admitted


__all__ = [
    "DOOR_NAME",
    "RECORD_FIELDS",
    "STAMP_HEADER_NAMES",
    "AdmissionRefusedError",
    "DecodedRecord",
    "admit_batch",
    "admit_record",
    "batch_records",
    "effective_limit",
    "kafka_record_size",
    "validate_shape",
]
