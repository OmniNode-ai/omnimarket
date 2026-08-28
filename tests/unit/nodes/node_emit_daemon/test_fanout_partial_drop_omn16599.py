# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16599: a fan-out leg the daemon discards must never ACK as ``queued``.

The reported symptom was ``onex.cmd.omniintelligence.*`` topics sitting at zero
records while the sibling ``onex.evt.*`` topics from the SAME emit received
traffic, with ``send_event()`` returning ``True`` and no error surfaced to the
caller.

The mechanism is in ``EmitSocketServer._handle_emit``: the fan-out loop treated
every per-leg failure as a ``continue``/``logger.warning`` and then computed the
reply from ``last_event_id``, which only a *successful* leg ever sets. Any
event registered with two or more fan-out topics therefore replied
``{"status": "queued"}`` -- which ``emit_client.send_event`` maps to ``True`` --
as long as ONE leg queued, even when the other leg was thrown away.

The loss is systematically biased toward the ``cmd`` leg because the registries
give the ``cmd`` topic the full ``passthrough`` payload while the ``evt`` topic
gets a ``strip_prompt``-reduced one (see ``prompt.submitted`` in
``registries/topics.yaml``), so the size check can only ever fire on the ``cmd``
side.

Fail-fast doctrine: ``send_event() == True`` with a leg dropped and no trace is
a defect regardless of whether dropping that leg is policy-correct. These tests
pin the invariant "every declared fan-out leg is queued, or the reply is an
error naming the legs that were not".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.event_registry import (
    EventRegistration,
    EventRegistry,
    FanOutRule,
    transform_strip_prompt,
)
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)
from omnimarket.nodes.node_emit_daemon.models.model_protocol import (
    ModelDaemonEmitRequest,
)
from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

CMD_TOPIC = "onex.cmd.omniintelligence.cursor-hook-event.v1"
EVT_TOPIC = "onex.evt.omnicursor.prompt-submitted.v1"


class _RecordingQueue(BoundedEventQueue):
    """Queue that records enqueued events and can refuse specific topics.

    ``refuse_topics`` reproduces ``BoundedEventQueue.enqueue`` returning False
    for a telemetry event (memory queue full with spooling disabled, or an event
    larger than ``max_spool_bytes``) without having to fill a real spool.
    """

    def __init__(self, refuse_topics: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self._refuse_topics = refuse_topics
        self.enqueued: list[ModelQueuedEvent] = []

    async def enqueue(self, event: ModelQueuedEvent) -> bool:
        if event.topic in self._refuse_topics:
            return False
        self.enqueued.append(event)
        return True


def _two_leg_registry() -> EventRegistry:
    """cmd leg carries the full payload; evt leg is stripped -- the live shape."""
    return EventRegistry.from_dict(
        {
            "cursor.hook.prompt": EventRegistration(
                event_type="cursor.hook.prompt",
                fan_out=[
                    FanOutRule(
                        topic=CMD_TOPIC,
                        tier=EnumDurabilityTier.TELEMETRY,
                        transform=None,
                        description="Canonical event for intent classification",
                    ),
                    FanOutRule(
                        topic=EVT_TOPIC,
                        tier=EnumDurabilityTier.TELEMETRY,
                        transform=transform_strip_prompt,
                        description="Sanitized preview for observability",
                    ),
                ],
                partition_key_field="session_id",
                required_fields=["session_id"],
            )
        }
    )


def _server(
    queue: BoundedEventQueue,
    *,
    max_payload_bytes: int = 1_048_576,
) -> EmitSocketServer:
    return EmitSocketServer(
        socket_path="/nonexistent/omn16599.sock",
        queue=queue,
        registry=_two_leg_registry(),
        max_payload_bytes=max_payload_bytes,
    )


async def _emit(server: EmitSocketServer, payload: dict[str, Any]) -> dict[str, Any]:
    request = ModelDaemonEmitRequest(event_type="cursor.hook.prompt", payload=payload)
    raw = await server._handle_emit(request)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversize_cmd_leg_is_not_acked_as_queued() -> None:
    """The exact reported shape: cmd leg too large, evt leg stripped and small.

    Before OMN-16599 this returned ``{"status": "queued"}`` -- ``send_event()``
    True -- with the cmd topic receiving nothing.
    """
    queue = _RecordingQueue()
    server = _server(queue, max_payload_bytes=2048)

    response = await _emit(
        server,
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "prompt": "x" * 8192,
            "prompt_preview": "x" * 32,
        },
    )

    assert response["status"] == "error", (
        f"a discarded cmd fan-out leg must not be reported as queued; got {response!r}"
    )
    assert CMD_TOPIC in response["reason"]
    # The evt leg genuinely did land -- the reply must say so rather than
    # implying the whole emit was rejected.
    assert EVT_TOPIC in response["reason"]
    assert [e.topic for e in queue.enqueued] == [EVT_TOPIC]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refused_cmd_leg_is_not_acked_as_queued() -> None:
    """``enqueue`` returning False for one leg must not ACK the whole emit."""
    queue = _RecordingQueue(refuse_topics=frozenset({CMD_TOPIC}))
    server = _server(queue)

    response = await _emit(
        server,
        {
            "session_id": "22222222-2222-2222-2222-222222222222",
            "prompt": "hello",
            "prompt_preview": "hello",
        },
    )

    assert response["status"] == "error", (
        f"a refused cmd fan-out leg must not be reported as queued; got {response!r}"
    )
    assert CMD_TOPIC in response["reason"]
    assert [e.topic for e in queue.enqueued] == [EVT_TOPIC]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unserializable_leg_is_not_acked_as_queued() -> None:
    """A leg whose transformed payload cannot be serialized must fail loudly."""

    class _Unserializable:
        pass

    def _explode(payload: dict[str, object]) -> dict[str, object]:
        result = dict(payload)
        result["boom"] = _Unserializable()
        return result

    registry = EventRegistry.from_dict(
        {
            "cursor.hook.prompt": EventRegistration(
                event_type="cursor.hook.prompt",
                fan_out=[
                    FanOutRule(
                        topic=CMD_TOPIC,
                        tier=EnumDurabilityTier.TELEMETRY,
                        transform=_explode,
                    ),
                    FanOutRule(
                        topic=EVT_TOPIC,
                        tier=EnumDurabilityTier.TELEMETRY,
                        transform=None,
                    ),
                ],
                partition_key_field="session_id",
                required_fields=["session_id"],
            )
        }
    )
    queue = _RecordingQueue()
    server = EmitSocketServer(
        socket_path="/nonexistent/omn16599.sock",
        queue=queue,
        registry=registry,
    )

    response = await _emit(
        server, {"session_id": "33333333-3333-3333-3333-333333333333"}
    )

    assert response["status"] == "error", (
        f"an unserializable fan-out leg must not be reported as queued; got {response!r}"
    )
    assert CMD_TOPIC in response["reason"]
    assert [e.topic for e in queue.enqueued] == [EVT_TOPIC]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_legs_queued_still_acks_queued() -> None:
    """Guard against over-correction: a fully successful fan-out still ACKs."""
    queue = _RecordingQueue()
    server = _server(queue)

    response = await _emit(
        server,
        {
            "session_id": "44444444-4444-4444-4444-444444444444",
            "prompt": "hello",
            "prompt_preview": "hello",
        },
    )

    assert response["status"] == "queued"
    assert response["event_id"]
    assert sorted(e.topic for e in queue.enqueued) == sorted([CMD_TOPIC, EVT_TOPIC])
