# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared in-memory bus round-trip driver for WS-5 Wave 7 orchestrator tests.

Drives an ONEX orchestrator handler over ``EventBusInmemory`` via
``LocalRuntimeBusAdapter``: subscribes the adapter on the start topic, publishes
a start command, and returns the terminal-event history from the output topic.

No live Kafka / .201 — this is the canonical CI technique from the
2026-06-27 market-skill integration scout report (§5.2 Variant B).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tests.runtime_local_compat import LocalRuntimeBusAdapter


async def drive_round_trip(
    bus: Any,
    *,
    handler: Any,
    handler_name: str,
    input_model_cls: type[BaseModel] | None,
    start_topic: str,
    output_topic: str,
    payload_bytes: bytes,
    group_id: str,
) -> list[Any]:
    """Publish ``payload_bytes`` to ``start_topic`` and return ``output_topic`` history.

    The adapter deserializes the payload, invokes ``handler.handle`` (sync or
    async), serializes the result, and republishes to ``output_topic``. Returns
    the terminal-event history list (empty when the handler raised or returned
    ``None`` — the boundary-rejection signal used as a negative control).
    """
    await bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=handler,
            handler_name=handler_name,
            input_model_cls=input_model_cls,
            output_topic=output_topic,
            bus=bus,
        )
        await bus.subscribe(
            start_topic,
            on_message=adapter.on_message,
            group_id=group_id,
        )
        await bus.publish(start_topic, None, payload_bytes)
        return await bus.get_event_history(topic=output_topic)
    finally:
        await bus.close()
