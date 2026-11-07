# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical DLQ routing for projection runners (OMN-13548 / D-03).

Projection runners that catch a ``ValidationError`` on a malformed delegation /
savings event previously logged at ERROR and dropped the message silently — no
DLQ, no failure event, no terminal. A malformed event therefore vanished from
the bus with no durable trace.

This module supplies the canonical, reused DLQ surface so every projection
runner emits a DURABLE failure signal ON THE BUS instead of dropping:

* the DLQ topic is read from the node's ``contract.yaml`` under
  ``event_bus.dlq_topics`` — never invented inline (mirrors the canonical
  ``node_intent_event_consumer_effect`` pattern, which routes to
  ``config.dlq_topics[0]``);
* the DLQ envelope shape matches that handler verbatim
  (``original_message`` / ``failure_reason`` / ``retry_count`` / ``failed_at`` /
  ``handler``) so a single DLQ consumer can drain every producer; and
* the failure carries the offending event's ``correlation_id`` so the dropped
  payload is recoverable by correlation; and
* when the caller holds the record's ``MessageMeta``, the envelope names the
  source topic, partition and offset (OMN-21288), so a redrive can re-read the
  refused record from its source without resetting any consumer group's offset.

A projection handler that can raise ``ValidationError`` without routing to a DLQ
topic is rejected by the ``dlq`` rule of ``node_contract_projection_check_compute`` (CI +
pre-commit).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omnimarket.handlers.dlq_envelope_bound import (
    _ENVELOPE_RESERVE_BYTES,
    _MAX_FAILURE_REASON_CHARS,
    DLQ_MAX_ENVELOPE_BYTES,
    bound_dlq_original_message,
)
from omnimarket.models.model_message_meta import MessageMeta

logger = logging.getLogger(__name__)

# Type of the async publish callable shared with the projection runners:
# (topic, value_bytes) -> awaitable[None].
PublishFn = Callable[[str, bytes], Awaitable[None]]


def dlq_topics_from_contract(contract: dict[str, Any]) -> list[str]:
    """Read the contract-declared DLQ topics (``event_bus.dlq_topics``).

    The contract is the single source of truth for topic declarations; the DLQ
    topic must never be hardcoded in the handler. Returns the declared list (may
    be empty if the contract declares none).
    """
    event_bus = contract.get("event_bus", {})
    topics = event_bus.get("dlq_topics", [])
    return [str(t) for t in topics]


def _resolve_correlation_id(
    original_message: dict[str, Any],
    correlation_id: str | None,
    source: MessageMeta | None,
) -> str:
    """The caller's id, else the one the payload carries, else the record's own."""
    if correlation_id is not None:
        return correlation_id
    fallback = source.fallback_id if source is not None else ""
    return correlation_id_from_payload(original_message, fallback=fallback)


def build_dlq_envelope(
    *,
    original_message: dict[str, Any],
    failure_reason: str,
    handler: str,
    correlation_id: str | None = None,
    retry_count: int = 0,
    source: MessageMeta | None = None,
) -> dict[str, Any]:
    """Build the canonical DLQ envelope for a dropped projection event.

    Mirrors ``node_intent_event_consumer_effect._route_to_dlq`` so a single DLQ
    consumer drains every producer. ``correlation_id`` is hoisted to the top
    level (in addition to riding inside ``original_message``) so the failure is
    recoverable by correlation even when the offending payload is unparseable.
    ``source``, when given, adds ``source_topic`` / ``source_partition`` /
    ``source_offset`` naming the record the event was read from (OMN-21288).
    """
    correlation_id = _resolve_correlation_id(original_message, correlation_id, source)
    bounded, truncated = bound_dlq_original_message(
        original_message, max_bytes=DLQ_MAX_ENVELOPE_BYTES - _ENVELOPE_RESERVE_BYTES
    )
    envelope: dict[str, Any] = {
        "original_message": bounded,
        "failure_reason": failure_reason[:_MAX_FAILURE_REASON_CHARS],
        "correlation_id": correlation_id,
        "retry_count": retry_count,
        "failed_at": datetime.now(UTC).isoformat(),
        "handler": handler,
    }
    if source is not None:
        envelope["source_topic"] = source.topic
        envelope["source_partition"] = source.partition
        envelope["source_offset"] = source.offset
    if truncated:
        envelope["original_message_truncated"] = True
        envelope["original_message_bytes"] = len(
            json.dumps(original_message, default=str).encode("utf-8")
        )
    return envelope


async def route_to_dlq(
    *,
    publish: PublishFn | None,
    dlq_topics: list[str],
    original_message: dict[str, Any],
    failure_reason: str,
    handler: str,
    correlation_id: str | None = None,
    retry_count: int = 0,
    source: MessageMeta | None = None,
) -> bool:
    """Publish a malformed projection event to the contract-declared DLQ topic.

    Returns ``True`` when the DLQ envelope was published, ``False`` when no DLQ
    topic is declared or no publisher is available (the message is then logged at
    ERROR — the prior silent-drop behavior — but the caller has emitted the
    strongest signal it can). Best-effort: a DLQ publish failure is logged, not
    raised, so it never wedges the consumer.
    """
    correlation_id = _resolve_correlation_id(original_message, correlation_id, source)
    if not dlq_topics:
        logger.error(
            "projection handler %s dropped a malformed event with NO DLQ topic "
            "declared (correlation_id=%s): %s",
            handler,
            correlation_id,
            failure_reason,
        )
        return False

    dlq_topic = dlq_topics[0]
    if publish is None:
        logger.error(
            "projection handler %s would route malformed event to DLQ %s but has "
            "no publisher (correlation_id=%s): %s",
            handler,
            dlq_topic,
            correlation_id,
            failure_reason,
        )
        return False

    envelope = build_dlq_envelope(
        original_message=original_message,
        failure_reason=failure_reason,
        handler=handler,
        correlation_id=correlation_id,
        retry_count=retry_count,
        source=source,
    )
    value = json.dumps(envelope, default=str).encode("utf-8")
    try:
        await publish(dlq_topic, value)
    except Exception as exc:
        logger.error(
            "projection handler %s failed to route malformed event to DLQ %s "
            "(correlation_id=%s): %s",
            handler,
            dlq_topic,
            correlation_id,
            exc,
        )
        return False

    logger.warning(
        "projection handler %s routed malformed event to DLQ %s "
        "(correlation_id=%s): %s",
        handler,
        dlq_topic,
        correlation_id,
        failure_reason,
    )
    return True


def correlation_id_from_payload(payload: dict[str, Any], fallback: str = "") -> str:
    """Best-effort extract a correlation id from a (possibly malformed) payload."""
    value = (
        payload.get("correlation_id")
        or payload.get("correlationId")
        or payload.get("session_id")
        or payload.get("sessionId")
        or fallback
    )
    return str(value) if value is not None else fallback


__all__ = [
    "DLQ_MAX_ENVELOPE_BYTES",
    "PublishFn",
    "bound_dlq_original_message",
    "build_dlq_envelope",
    "correlation_id_from_payload",
    "dlq_topics_from_contract",
    "route_to_dlq",
]
