# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_event_emit_effect (OMN-15965 R1).

End-to-end proof from HandlerEventEmitEffect.handle() through the spool
outbox, a publish adapter, and a real EventBusInmemory readback -- the full
wire shape a caller would see, with zero external infra.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    ProtocolPublishAdapter,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

pytestmark = pytest.mark.unit


class _RecordingAdapter:
    """Records publish calls in-process; the golden chain replays them onto
    a real EventBusInmemory to prove the wire shape round-trips.

    Records ``correlation_id`` alongside each message so tests can prove the
    adapter actually received the original correlation ID, not just that
    ``ModelEmitResult.correlation_id`` echoes the request.
    """

    def __init__(self) -> None:
        self.received: list[tuple[str, bytes]] = []
        self.received_correlation_ids: list[str | None] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        self.received.append((topic, json.dumps(payload).encode("utf-8")))
        self.received_correlation_ids.append(correlation_id)


class TestGoldenChainEventEmitEffect:
    async def test_session_started_publishes_end_to_end(
        self, tmp_path: Path, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()

        recorder = _RecordingAdapter()
        adapter: ProtocolPublishAdapter = recorder
        spool = SpoolOutbox(tmp_path / "spool")
        handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

        request = ModelEmitRequest(
            event_type="session.started",
            payload={"session_id": "golden-chain-session"},
            correlation_id="corr-golden-1",
        )
        result = handler.handle(request)

        assert result.published is True
        assert result.topics_published == ["onex.evt.omniclaude.session-started.v1"]
        assert result.correlation_id == "corr-golden-1"
        assert spool.pending_count() == 0  # acked -- no leftover file
        # The adapter itself received the original correlation_id -- not
        # just an echo on ModelEmitResult.
        assert recorder.received_correlation_ids == ["corr-golden-1"]

        # Now drive the same payload through a real event bus, proving the
        # wire shape (topic + JSON-serialized payload) round-trips.
        for topic, value in recorder.received:
            await event_bus.publish(topic, key=None, value=value)

        history = await event_bus.get_event_history(
            topic="onex.evt.omniclaude.session-started.v1"
        )
        assert len(history) == 1
        deserialized = json.loads(history[0].value)
        assert deserialized["session_id"] == "golden-chain-session"

        await event_bus.close()

    async def test_prompt_submitted_fans_out_to_both_topics_on_the_bus(
        self, tmp_path: Path, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()

        recorder = _RecordingAdapter()
        adapter: ProtocolPublishAdapter = recorder
        spool = SpoolOutbox(tmp_path / "spool")
        handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

        request = ModelEmitRequest(
            event_type="prompt.submitted",
            payload={"session_id": "s1", "prompt_preview": "hello"},
        )
        result = handler.handle(request)

        assert result.published is True
        assert set(result.topics_published) == {
            "onex.cmd.omniintelligence.claude-hook-event.v1",
            "onex.evt.omniclaude.prompt-submitted.v1",
        }

        for topic, value in recorder.received:
            await event_bus.publish(topic, key=None, value=value)

        for topic in result.topics_published:
            history = await event_bus.get_event_history(topic=topic)
            assert len(history) == 1

        await event_bus.close()

    async def test_spool_backlog_drains_onto_the_bus_across_invocations(
        self, tmp_path: Path, event_bus: EventBusInmemory
    ) -> None:
        """Proves the durability story end-to-end: a backlog record spooled
        by one invocation gets drained and published on the next, landing on
        the real bus with its original event_id/correlation_id intact."""
        await event_bus.start()
        spool = SpoolOutbox(tmp_path / "spool")

        # First invocation: Kafka unavailable (no adapter) -- spool-only.
        offline_handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)
        backlog_request = ModelEmitRequest(
            event_type="session.ended",
            payload={"session_id": "backlog-session"},
            correlation_id="corr-backlog-1",
        )
        offline_result = offline_handler.handle(backlog_request)
        assert offline_result.published is False
        assert spool.pending_count() == 1

        # Second invocation: Kafka is back -- current event publishes AND the
        # backlog drains, both reaching the real bus.
        recorder = _RecordingAdapter()
        online_handler = HandlerEventEmitEffect(spool=spool, publish_adapter=recorder)
        current_result = online_handler.handle(
            ModelEmitRequest(
                event_type="session.started", payload={"session_id": "current-session"}
            )
        )
        assert current_result.published is True
        assert current_result.drained_count == 1
        assert spool.pending_count() == 0
        # The drained backlog record's original correlation_id reached the
        # adapter unchanged -- proves the idempotency key survives the retry.
        assert "corr-backlog-1" in recorder.received_correlation_ids

        for topic, value in recorder.received:
            await event_bus.publish(topic, key=None, value=value)

        backlog_history = await event_bus.get_event_history(
            topic="onex.evt.omniclaude.session-ended.v1"
        )
        assert len(backlog_history) == 1
        assert json.loads(backlog_history[0].value)["session_id"] == "backlog-session"

        await event_bus.close()
