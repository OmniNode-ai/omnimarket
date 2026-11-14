# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name the source record in a projection dead-letter envelope (OMN-22811).

A redrive reads a refused record again from the topic, partition and offset the
consumer read it from, so the envelope carries them. They ride in the envelope
only when the consumer knows the topic.
"""

from __future__ import annotations

import json
from typing import Any

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.projection.dlq import (
    PublishFn,
    build_dlq_envelope,
    correlation_id_from_payload,
    route_to_dlq,
)


def source_coordinates(meta: MessageMeta | None) -> dict[str, Any]:
    """The envelope fields naming the record ``meta`` was read from, or none."""
    if meta is None or not meta.topic:
        return {}
    return {
        "source_topic": meta.topic,
        "source_partition": meta.partition,
        "source_offset": meta.offset,
    }


def build_envelope_with_source(
    meta: MessageMeta | None, **envelope_args: Any
) -> dict[str, Any]:
    """``build_dlq_envelope`` for a record read at ``meta``."""
    return {**build_dlq_envelope(**envelope_args), **source_coordinates(meta)}


async def route_malformed_with_source(
    *,
    publish: PublishFn | None,
    dlq_topics: list[str],
    data: dict[str, Any],
    failure_reason: str,
    handler: str,
    meta: MessageMeta | None,
) -> bool:
    """``route_to_dlq`` for a record read at ``meta``, correlated by its payload."""
    coordinates = source_coordinates(meta)
    send = publish

    async def publish_named(dlq_topic: str, value: bytes) -> None:
        assert send is not None
        envelope = {**json.loads(value.decode("utf-8")), **coordinates}
        await send(dlq_topic, json.dumps(envelope, default=str).encode("utf-8"))

    return await route_to_dlq(
        publish=publish_named if publish is not None else None,
        dlq_topics=dlq_topics,
        original_message=data,
        failure_reason=failure_reason,
        handler=handler,
        correlation_id=correlation_id_from_payload(
            data, fallback=meta.fallback_id if meta is not None else ""
        ),
    )
