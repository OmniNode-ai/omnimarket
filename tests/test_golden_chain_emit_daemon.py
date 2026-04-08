# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_emit_daemon.

Verifies the full local path: client -> socket -> queue -> publisher (mock Kafka).
All tests run without infrastructure (no real Kafka, no real socket server for
unit tests).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnimarket.nodes.node_emit_daemon.client import EmitClient, default_socket_path
from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.event_registry import (
    EventRegistration,
    EventRegistry,
    FanOutRule,
    transform_passthrough,
    transform_strip_body,
    transform_strip_prompt,
)
from omnimarket.nodes.node_emit_daemon.handlers.handler_emit_daemon import (
    HandlerEmitDaemon,
)
from omnimarket.nodes.node_emit_daemon.models.model_daemon_state import (
    EnumEmitDaemonPhase,
)
from omnimarket.nodes.node_emit_daemon.models.model_protocol import (
    ModelDaemonEmitRequest,
    ModelDaemonErrorResponse,
    ModelDaemonPingRequest,
    ModelDaemonPingResponse,
    ModelDaemonQueuedResponse,
    parse_daemon_request,
    parse_daemon_response,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop
from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

# =============================================================================
# Contract Tests
# =============================================================================


class TestContract:
    """Verify contract.yaml and metadata.yaml exist and parse."""

    def test_contract_yaml_exists(self) -> None:
        contract_path = (
            Path(__file__).parent.parent
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_emit_daemon"
            / "contract.yaml"
        )
        assert contract_path.exists()
        import yaml

        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert contract["name"] == "emit_daemon"
        assert contract["node_type"] == "service"

    def test_metadata_yaml_exists(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_emit_daemon"
            / "metadata.yaml"
        )
        assert metadata_path.exists()
        import yaml

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)
        assert metadata["name"] == "node_emit_daemon"


# =============================================================================
# Protocol Model Tests
# =============================================================================


class TestProtocolModels:
    """Test socket protocol request/response models."""

    def test_parse_ping_request(self) -> None:
        req = parse_daemon_request({"command": "ping"})
        assert isinstance(req, ModelDaemonPingRequest)

    def test_parse_emit_request(self) -> None:
        req = parse_daemon_request(
            {"event_type": "session.started", "payload": {"session_id": "abc"}}
        )
        assert isinstance(req, ModelDaemonEmitRequest)
        assert req.event_type == "session.started"

    def test_parse_ambiguous_request_raises(self) -> None:
        with pytest.raises(ValueError, match="Ambiguous"):
            parse_daemon_request({"command": "ping", "event_type": "test"})

    def test_parse_invalid_request_raises(self) -> None:
        with pytest.raises(ValueError, match="must contain"):
            parse_daemon_request({"foo": "bar"})

    def test_parse_ping_response(self) -> None:
        resp = parse_daemon_response({"status": "ok", "queue_size": 5, "spool_size": 2})
        assert isinstance(resp, ModelDaemonPingResponse)
        assert resp.queue_size == 5

    def test_parse_queued_response(self) -> None:
        resp = parse_daemon_response({"status": "queued", "event_id": "evt-123"})
        assert isinstance(resp, ModelDaemonQueuedResponse)
        assert resp.event_id == "evt-123"

    def test_parse_error_response(self) -> None:
        resp = parse_daemon_response({"status": "error", "reason": "bad request"})
        assert isinstance(resp, ModelDaemonErrorResponse)
        assert resp.reason == "bad request"


# =============================================================================
# Event Queue Tests
# =============================================================================


class TestBoundedEventQueue:
    """Test BoundedEventQueue with disk spool."""

    def _make_event(self, event_id: str = "evt-1") -> ModelQueuedEvent:
        return ModelQueuedEvent(
            event_id=event_id,
            event_type="test.event",
            topic="onex.evt.test.v1",
            payload={"key": "value"},
            queued_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_enqueue_dequeue_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(max_memory_queue=10, spool_dir=Path(tmpdir))
            event = self._make_event()
            assert await queue.enqueue(event)
            assert queue.memory_size() == 1

            dequeued = await queue.dequeue()
            assert dequeued is not None
            assert dequeued.event_id == "evt-1"
            assert queue.memory_size() == 0

    @pytest.mark.asyncio
    async def test_overflow_to_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(
                max_memory_queue=1,
                max_spool_messages=10,
                spool_dir=Path(tmpdir),
            )
            await queue.enqueue(self._make_event("evt-1"))
            await queue.enqueue(self._make_event("evt-2"))

            assert queue.memory_size() == 1
            assert queue.spool_size() == 1

    @pytest.mark.asyncio
    async def test_dequeue_from_spool_after_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(
                max_memory_queue=1,
                max_spool_messages=10,
                spool_dir=Path(tmpdir),
            )
            await queue.enqueue(self._make_event("evt-1"))
            await queue.enqueue(self._make_event("evt-2"))

            e1 = await queue.dequeue()
            assert e1 is not None
            assert e1.event_id == "evt-1"

            e2 = await queue.dequeue()
            assert e2 is not None
            assert e2.event_id == "evt-2"

    @pytest.mark.asyncio
    async def test_drain_to_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(max_memory_queue=10, spool_dir=Path(tmpdir))
            await queue.enqueue(self._make_event("evt-1"))
            await queue.enqueue(self._make_event("evt-2"))

            count = await queue.drain_to_spool()
            assert count == 2
            assert queue.memory_size() == 0
            assert queue.spool_size() == 2

    @pytest.mark.asyncio
    async def test_load_spool_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            spool_dir = Path(tmpdir)
            queue1 = BoundedEventQueue(max_memory_queue=10, spool_dir=spool_dir)
            await queue1.enqueue(self._make_event("evt-1"))
            await queue1.drain_to_spool()

            queue2 = BoundedEventQueue(max_memory_queue=10, spool_dir=spool_dir)
            count = await queue2.load_spool()
            assert count == 1

    @pytest.mark.asyncio
    async def test_empty_dequeue_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            assert await queue.dequeue() is None

    @pytest.mark.asyncio
    async def test_spooling_disabled_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(
                max_memory_queue=1,
                max_spool_messages=0,
                max_spool_bytes=0,
                spool_dir=Path(tmpdir),
            )
            assert await queue.enqueue(self._make_event("evt-1"))
            assert not await queue.enqueue(self._make_event("evt-2"))


# =============================================================================
# Event Registry Tests
# =============================================================================


class TestEventRegistry:
    """Test pluggable event registry."""

    def test_from_dict(self) -> None:
        reg = EventRegistration(
            event_type="test.event",
            fan_out=[FanOutRule(topic="onex.evt.test.v1")],
            required_fields=["session_id"],
        )
        registry = EventRegistry.from_dict({"test.event": reg})
        assert registry.get_registration("test.event") is not None
        assert registry.get_registration("unknown") is None

    def test_validate_payload(self) -> None:
        reg = EventRegistration(
            event_type="test.event",
            fan_out=[],
            required_fields=["session_id", "name"],
        )
        registry = EventRegistry.from_dict({"test.event": reg})
        missing = registry.validate_payload("test.event", {"session_id": "abc"})
        assert missing == ["name"]

    def test_validate_payload_unknown_type(self) -> None:
        registry = EventRegistry()
        with pytest.raises(KeyError):
            registry.validate_payload("unknown", {})

    def test_get_partition_key(self) -> None:
        reg = EventRegistration(
            event_type="test.event",
            partition_key_field="session_id",
        )
        registry = EventRegistry.from_dict({"test.event": reg})
        key = registry.get_partition_key("test.event", {"session_id": "abc123"})
        assert key == "abc123"

    def test_from_yaml(self) -> None:
        registry_path = (
            Path(__file__).parent.parent
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_emit_daemon"
            / "registries"
            / "claude_code.yaml"
        )
        registry = EventRegistry.from_yaml(registry_path)
        assert len(registry) > 40  # Should have 40+ event types
        assert registry.get_registration("session.started") is not None
        assert registry.get_registration("prompt.submitted") is not None

    def test_list_event_types(self) -> None:
        reg = EventRegistration(event_type="a", fan_out=[])
        registry = EventRegistry.from_dict({"a": reg})
        assert registry.list_event_types() == ["a"]


# =============================================================================
# Transform Tests
# =============================================================================


class TestTransforms:
    """Test payload transform functions."""

    def test_passthrough(self) -> None:
        payload: dict[str, object] = {"key": "value"}
        result = transform_passthrough(payload)
        assert result == payload

    def test_strip_prompt_removes_full_prompt(self) -> None:
        payload: dict[str, object] = {
            "prompt": "This is a long prompt",
            "session_id": "abc",
        }
        result = transform_strip_prompt(payload)
        assert "prompt" not in result
        assert "prompt_preview" in result
        assert "prompt_length" in result

    def test_strip_prompt_removes_b64(self) -> None:
        payload: dict[str, object] = {
            "prompt_b64": "base64data",
            "prompt_preview": "short",
            "prompt_length": 42,
        }
        result = transform_strip_prompt(payload)
        assert "prompt_b64" not in result
        assert result["prompt_preview"] == "short"

    def test_strip_body(self) -> None:
        payload: dict[str, object] = {
            "body": "Full message body here",
            "session_id": "abc",
        }
        result = transform_strip_body(payload)
        assert "body" not in result
        assert result["body_length"] == 22
        assert "body_preview" in result


# =============================================================================
# Handler FSM Tests
# =============================================================================


class TestHandlerEmitDaemon:
    """Test lifecycle FSM handler."""

    def test_initial_state_is_idle(self) -> None:
        handler = HandlerEmitDaemon()
        assert handler.phase == EnumEmitDaemonPhase.IDLE

    def test_full_lifecycle(self) -> None:
        handler = HandlerEmitDaemon()
        handler.transition_to_binding("/tmp/test.sock", 12345)
        assert handler.phase == EnumEmitDaemonPhase.BINDING

        handler.transition_to_listening()
        assert handler.phase == EnumEmitDaemonPhase.LISTENING

        handler.transition_to_draining()
        assert handler.phase == EnumEmitDaemonPhase.DRAINING

        event = handler.transition_to_stopped(events_published=10, events_dropped=2)
        assert handler.phase == EnumEmitDaemonPhase.STOPPED
        assert event.events_published == 10

    def test_invalid_transition_raises(self) -> None:
        handler = HandlerEmitDaemon()
        with pytest.raises(ValueError, match="Cannot transition"):
            handler.transition_to_listening()

    def test_circuit_breaker(self) -> None:
        handler = HandlerEmitDaemon()
        assert not handler.is_circuit_broken()

        for i in range(3):
            handler.transition_to_failed(f"error {i}")
        assert handler.is_circuit_broken()

    def test_reset(self) -> None:
        handler = HandlerEmitDaemon()
        handler.transition_to_binding("/tmp/test.sock", 123)
        handler.reset()
        assert handler.phase == EnumEmitDaemonPhase.IDLE


# =============================================================================
# Publisher Loop Tests
# =============================================================================


class TestKafkaPublisherLoop:
    """Test Kafka publisher loop with mock publish_fn."""

    def _make_event(self, event_id: str = "evt-1") -> ModelQueuedEvent:
        return ModelQueuedEvent(
            event_id=event_id,
            event_type="test.event",
            topic="onex.evt.test.v1",
            payload={"session_id": "abc", "key": "value"},
            queued_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_publish_single_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            mock_publish = AsyncMock()
            publisher = KafkaPublisherLoop(queue=queue, publish_fn=mock_publish)

            await queue.enqueue(self._make_event())
            await publisher.start()
            await asyncio.sleep(0.3)
            await publisher.stop()

            assert mock_publish.call_count == 1
            assert publisher.events_published == 1

    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            call_count = 0

            async def _failing_then_ok(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("transient")

            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_failing_then_ok,
                max_retry_attempts=3,
                backoff_base_seconds=0.05,
            )

            await queue.enqueue(self._make_event())
            await publisher.start()
            await asyncio.sleep(0.5)
            await publisher.stop()

            assert call_count >= 2  # At least 1 fail + 1 success

    @pytest.mark.asyncio
    async def test_drop_after_exhausted_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))

            async def _always_fail(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                raise ConnectionError("permanent")

            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_always_fail,
                max_retry_attempts=2,
                backoff_base_seconds=0.01,
            )

            await queue.enqueue(self._make_event())
            await publisher.start()
            await asyncio.sleep(0.5)
            await publisher.stop()

            assert publisher.events_dropped == 1


# =============================================================================
# Socket Server Integration Test
# =============================================================================


class TestSocketServerIntegration:
    """Integration test: client -> socket -> queue."""

    @staticmethod
    async def _run_in_thread(fn: object, *args: object) -> object:
        """Run a sync function in a thread so the event loop stays free."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_client_to_server_emit(self) -> None:
        """Full local path: client -> socket -> queue (no Kafka)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-emit.sock")
            spool_dir = Path(tmpdir) / "spool"

            registry = EventRegistry.from_dict(
                {
                    "test.event": EventRegistration(
                        event_type="test.event",
                        fan_out=[
                            FanOutRule(topic="onex.evt.test.v1"),
                        ],
                        partition_key_field="session_id",
                        required_fields=["session_id"],
                    ),
                }
            )

            queue = BoundedEventQueue(spool_dir=spool_dir)
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
            )

            await server.start()

            try:
                client = EmitClient(socket_path=socket_path, timeout=2.0)
                try:
                    event_id = await self._run_in_thread(
                        client.emit_sync,
                        "test.event",
                        {"session_id": "test-session-123"},
                    )
                    assert event_id
                    assert queue.memory_size() == 1
                finally:
                    client.close()
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_client_ping(self) -> None:
        """Test ping request through socket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-ping.sock")
            spool_dir = Path(tmpdir) / "spool"

            registry = EventRegistry()
            queue = BoundedEventQueue(spool_dir=spool_dir)
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
            )

            await server.start()
            try:
                client = EmitClient(socket_path=socket_path, timeout=2.0)
                try:
                    result = await self._run_in_thread(
                        client.is_daemon_running_sync,
                    )
                    assert result
                finally:
                    client.close()
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_unknown_event_type_rejected(self) -> None:
        """Events with unknown types are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-reject.sock")
            spool_dir = Path(tmpdir) / "spool"

            registry = EventRegistry()  # Empty registry
            queue = BoundedEventQueue(spool_dir=spool_dir)
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
            )

            await server.start()
            try:
                client = EmitClient(socket_path=socket_path, timeout=2.0)
                try:
                    with pytest.raises(ValueError, match="Daemon rejected"):
                        await self._run_in_thread(
                            client.emit_sync,
                            "unknown.event",
                            {"session_id": "test"},
                        )
                finally:
                    client.close()
            finally:
                await server.stop()


# =============================================================================
# Full Pipeline Integration Test (Proof of Life)
# =============================================================================


class TestProofOfLife:
    """Full pipeline: client -> socket -> queue -> publisher (mock Kafka)."""

    @pytest.mark.asyncio
    async def test_full_local_pipeline(self) -> None:
        """Proof of life: event flows from client through entire pipeline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "proof.sock")
            spool_dir = Path(tmpdir) / "spool"

            published_events: list[tuple[str, bytes]] = []

            async def mock_publish(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                published_events.append((topic, value))

            registry = EventRegistry.from_dict(
                {
                    "session.started": EventRegistration(
                        event_type="session.started",
                        fan_out=[
                            FanOutRule(topic="onex.evt.omniclaude.session-started.v1"),
                        ],
                        partition_key_field="session_id",
                        required_fields=["session_id"],
                    ),
                }
            )

            queue = BoundedEventQueue(spool_dir=spool_dir)
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
            )
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=mock_publish,
            )

            await server.start()
            await publisher.start()

            try:
                client = EmitClient(socket_path=socket_path, timeout=2.0)
                try:
                    loop = asyncio.get_running_loop()
                    event_id = await loop.run_in_executor(
                        None,
                        client.emit_sync,
                        "session.started",
                        {"session_id": "proof-of-life-session"},
                    )
                    assert event_id

                    # Wait for publisher to pick up the event
                    await asyncio.sleep(0.3)

                    assert len(published_events) == 1
                    topic, value_bytes = published_events[0]
                    assert topic == "onex.evt.omniclaude.session-started.v1"

                    payload = json.loads(value_bytes)
                    assert payload["session_id"] == "proof-of-life-session"
                finally:
                    client.close()
            finally:
                await publisher.stop()
                await server.stop()

            assert publisher.events_published == 1
            assert publisher.events_dropped == 0


# =============================================================================
# Client Tests
# =============================================================================


class TestEmitClient:
    """Test emit client default socket path resolution."""

    def test_default_socket_path_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEX_EMIT_SOCKET_PATH", "/custom/path.sock")
        assert default_socket_path() == "/custom/path.sock"

    def test_default_socket_path_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert default_socket_path() == "/run/user/1000/onex/emit.sock"

    def test_default_socket_path_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert default_socket_path() == "/tmp/onex-emit.sock"


# =============================================================================
# Circuit Breaker Tests
# =============================================================================


class TestCircuitBreaker:
    """Test circuit breaker in KafkaPublisherLoop."""

    def _make_event(self, event_id: str = "evt-1") -> ModelQueuedEvent:
        return ModelQueuedEvent(
            event_id=event_id,
            event_type="test.event",
            topic="onex.evt.test.v1",
            payload={"session_id": "abc", "key": "value"},
            queued_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self) -> None:
        """Circuit opens after consecutive failures reach threshold."""
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            EnumCircuitBreakerState,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))

            async def _always_fail(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                raise ConnectionError("Kafka down")

            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_always_fail,
                max_retry_attempts=1,
                backoff_base_seconds=0.01,
                max_backoff_seconds=0.02,
                failure_threshold=3,
                recovery_timeout=60.0,  # Long timeout so it stays OPEN
            )

            # Enqueue enough events to trigger the circuit breaker
            for i in range(5):
                await queue.enqueue(self._make_event(f"evt-{i}"))

            await publisher.start()
            # Wait for events to be processed and circuit to open
            for _ in range(100):
                await asyncio.sleep(0.02)
                if publisher.circuit_state == EnumCircuitBreakerState.OPEN:
                    break
            await publisher.stop(drain_timeout=2.0)

            assert publisher.circuit_state == EnumCircuitBreakerState.OPEN
            assert publisher.consecutive_failures >= 3

    @pytest.mark.asyncio
    async def test_circuit_recovers(self) -> None:
        """Circuit recovers from OPEN -> HALF_OPEN -> CLOSED on success."""
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            EnumCircuitBreakerState,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            call_count = 0

            async def _fail_then_succeed(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                nonlocal call_count
                call_count += 1
                # Fail for first 4 calls, succeed after
                if call_count <= 4:
                    raise ConnectionError("Kafka down")

            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_fail_then_succeed,
                max_retry_attempts=1,
                backoff_base_seconds=0.01,
                max_backoff_seconds=0.02,
                failure_threshold=3,
                recovery_timeout=0.1,  # Short recovery for test speed
            )

            for i in range(6):
                await queue.enqueue(self._make_event(f"evt-{i}"))

            await publisher.start()
            # Wait for recovery
            for _ in range(200):
                await asyncio.sleep(0.02)
                if publisher.circuit_state == EnumCircuitBreakerState.CLOSED:
                    break
            await publisher.stop(drain_timeout=2.0)

            assert publisher.circuit_state == EnumCircuitBreakerState.CLOSED
            assert publisher.events_published >= 1

    @pytest.mark.asyncio
    async def test_circuit_state_properties(self) -> None:
        """Publisher exposes circuit state properties for health endpoint."""
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            EnumCircuitBreakerState,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            mock_publish = AsyncMock()
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=mock_publish,
            )

            assert publisher.circuit_state == EnumCircuitBreakerState.CLOSED
            assert publisher.consecutive_failures == 0
            assert publisher.circuit_opened_at is None
            assert publisher.last_publish_at is None
            assert publisher.last_failure_at is None
            assert publisher.kafka_connected is True


# =============================================================================
# Health Endpoint Tests
# =============================================================================


class TestHealthEndpoint:
    """Test the health command through the socket server."""

    @pytest.mark.asyncio
    async def test_health_returns_detailed_status(self) -> None:
        """Health command returns ModelEmitDaemonHealth fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-health.sock")
            spool_dir = Path(tmpdir) / "spool"

            mock_publish = AsyncMock()
            registry = EventRegistry()
            queue = BoundedEventQueue(spool_dir=spool_dir)
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=mock_publish,
            )
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
                publisher_loop=publisher,
            )

            await server.start()
            await publisher.start()

            try:
                # Send health request via raw socket
                reader, writer = await asyncio.open_unix_connection(socket_path)
                try:
                    writer.write(json.dumps({"command": "health"}).encode() + b"\n")
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    health = json.loads(line)

                    assert health["healthy"] is True
                    assert health["circuit_state"] == "closed"
                    assert "memory_queue_size" in health
                    assert "spool_queue_size" in health
                    assert "events_published" in health
                    assert "events_dropped" in health
                    assert "events_buffered" in health
                    assert "uptime_seconds" in health
                finally:
                    writer.close()
                    await writer.wait_closed()
            finally:
                await publisher.stop(drain_timeout=2.0)
                await server.stop()

    @pytest.mark.asyncio
    async def test_health_responds_within_100ms(self) -> None:
        """Health endpoint must respond within 100ms budget."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-health-speed.sock")
            spool_dir = Path(tmpdir) / "spool"

            mock_publish = AsyncMock()
            registry = EventRegistry()
            queue = BoundedEventQueue(spool_dir=spool_dir)
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=mock_publish,
            )
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
                publisher_loop=publisher,
            )

            await server.start()
            await publisher.start()

            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                try:
                    start = datetime.now(UTC)
                    writer.write(json.dumps({"command": "health"}).encode() + b"\n")
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    elapsed_ms = (datetime.now(UTC) - start).total_seconds() * 1000

                    health = json.loads(line)
                    assert health["healthy"] is True
                    assert elapsed_ms < 100, (
                        f"Health took {elapsed_ms:.1f}ms, budget is 100ms"
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()
            finally:
                await publisher.stop(drain_timeout=2.0)
                await server.stop()

    @pytest.mark.asyncio
    async def test_health_client_method(self) -> None:
        """EmitClient.health_sync() returns health dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "test-health-client.sock")
            spool_dir = Path(tmpdir) / "spool"

            mock_publish = AsyncMock()
            registry = EventRegistry()
            queue = BoundedEventQueue(spool_dir=spool_dir)
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=mock_publish,
            )
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
                publisher_loop=publisher,
            )

            await server.start()
            await publisher.start()

            try:
                client = EmitClient(socket_path=socket_path, timeout=2.0)
                try:
                    loop = asyncio.get_running_loop()
                    health = await loop.run_in_executor(None, client.health_sync)
                    assert health["healthy"] is True
                    assert health["circuit_state"] == "closed"
                finally:
                    client.close()
            finally:
                await publisher.stop(drain_timeout=2.0)
                await server.stop()


# =============================================================================
# Golden Chain: Full Self-Healing Pipeline
# =============================================================================


class TestGoldenChainSelfHealing:
    """Full golden chain: emit -> publish -> health -> circuit open -> recover."""

    @pytest.mark.asyncio
    async def test_emit_publish_health_circuit_recovery(self) -> None:
        """Prove full self-healing path end-to-end."""
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            EnumCircuitBreakerState,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = str(Path(tmpdir) / "golden.sock")
            spool_dir = Path(tmpdir) / "spool"
            call_count = 0
            published_topics: list[str] = []

            async def _flaky_publish(
                topic: str,
                key: bytes | None,
                value: bytes,
                headers: dict[str, str],
            ) -> None:
                nonlocal call_count
                call_count += 1
                if 3 <= call_count <= 8:
                    raise ConnectionError("Kafka outage")
                published_topics.append(topic)

            registry = EventRegistry.from_dict(
                {
                    "test.event": EventRegistration(
                        event_type="test.event",
                        fan_out=[FanOutRule(topic="onex.evt.test.v1")],
                        partition_key_field="session_id",
                        required_fields=["session_id"],
                    ),
                }
            )

            queue = BoundedEventQueue(spool_dir=spool_dir)
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_flaky_publish,
                max_retry_attempts=1,
                backoff_base_seconds=0.01,
                max_backoff_seconds=0.02,
                failure_threshold=3,
                recovery_timeout=0.1,
            )
            server = EmitSocketServer(
                socket_path=socket_path,
                queue=queue,
                registry=registry,
                publisher_loop=publisher,
            )

            await server.start()
            await publisher.start()

            async def _send(
                request: dict[str, object],
            ) -> dict[str, object]:
                """Send a request on a fresh connection (avoids idle timeout)."""
                r, w = await asyncio.open_unix_connection(socket_path)
                try:
                    w.write(json.dumps(request).encode() + b"\n")
                    await w.drain()
                    line = await asyncio.wait_for(r.readline(), timeout=2.0)
                    return json.loads(line)  # type: ignore[no-any-return]
                finally:
                    w.close()
                    await w.wait_closed()

            try:
                # Phase 1: Emit and publish successfully
                for i in range(2):
                    resp = await _send(
                        {
                            "event_type": "test.event",
                            "payload": {"session_id": f"s-{i}"},
                        }
                    )
                    assert resp["status"] == "queued"

                await asyncio.sleep(0.3)
                assert len(published_topics) >= 1

                # Phase 2: Cause failures -> circuit opens
                for i in range(5):
                    resp = await _send(
                        {
                            "event_type": "test.event",
                            "payload": {"session_id": f"fail-{i}"},
                        }
                    )
                    assert resp["status"] == "queued"

                # Wait for circuit to open
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if publisher.circuit_state == EnumCircuitBreakerState.OPEN:
                        break

                # Verify health reports unhealthy
                health = await _send({"command": "health"})
                assert health["circuit_state"] in ("open", "half_open")

                # Phase 3: Restore publish_fn to always-succeed, then wait
                # for the circuit to reach at least HALF_OPEN (recovery timeout
                # is only 0.1s in the test).
                async def _always_succeed(
                    topic: str,
                    key: bytes | None,
                    value: bytes,
                    headers: dict[str, str],
                ) -> None:
                    published_topics.append(topic)

                publisher._publish_fn = _always_succeed

                # Drain any buffered events so the first HALF_OPEN probe
                # succeeds with _always_succeed.  Wait up to 10s total.
                for _ in range(200):
                    await asyncio.sleep(0.05)
                    if publisher.circuit_state == EnumCircuitBreakerState.CLOSED:
                        break

                if publisher.circuit_state != EnumCircuitBreakerState.CLOSED:
                    # Circuit hasn't closed yet (queue may be empty).
                    # Submit a fresh event so HALF_OPEN has something to probe.
                    resp = await _send(
                        {
                            "event_type": "test.event",
                            "payload": {"session_id": "recovery-probe"},
                        }
                    )
                    assert resp["status"] == "queued"

                    for _ in range(200):
                        await asyncio.sleep(0.05)
                        if publisher.circuit_state == EnumCircuitBreakerState.CLOSED:
                            break

                # Verify health is OK again
                health = await _send({"command": "health"})
                assert health["healthy"] is True
                assert health["circuit_state"] == "closed"

            finally:
                await publisher.stop(drain_timeout=2.0)
                await server.stop()


# =============================================================================
# New Model Tests
# =============================================================================


class TestNewModels:
    """Test ModelEmitDaemonConfig, ModelEmitDaemonHealth, ModelEmitEvent."""

    def test_config_defaults(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            ModelEmitDaemonConfig,
        )

        config = ModelEmitDaemonConfig()
        assert config.kafka_bootstrap_servers is None
        assert config.circuit_breaker_failure_threshold == 5
        assert config.circuit_breaker_recovery_timeout == 30.0
        assert config.max_memory_queue == 100

    def test_config_with_kafka(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            ModelEmitDaemonConfig,
        )

        config = ModelEmitDaemonConfig(
            kafka_bootstrap_servers="localhost:9092,localhost:9093"
        )
        assert config.kafka_bootstrap_servers == "localhost:9092,localhost:9093"

    def test_config_invalid_kafka_raises(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            ModelEmitDaemonConfig,
        )

        with pytest.raises(ValueError, match="Invalid bootstrap server"):
            ModelEmitDaemonConfig(kafka_bootstrap_servers="no-port")

    def test_health_model(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
            EnumCircuitBreakerState,
        )
        from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_health import (
            ModelEmitDaemonHealth,
        )

        health = ModelEmitDaemonHealth(
            healthy=True,
            circuit_state=EnumCircuitBreakerState.CLOSED,
            events_published=42,
            kafka_connected=True,
        )
        assert health.healthy
        dumped = json.loads(health.model_dump_json())
        assert dumped["events_published"] == 42

    def test_emit_event_model(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_emit_event import (
            ModelEmitEvent,
        )

        event = ModelEmitEvent(
            event_type="session.started",
            payload={"session_id": "abc"},
        )
        assert event.event_type == "session.started"
        assert event.event_id  # auto-generated
        assert event.received_at is not None

    def test_health_request_parse(self) -> None:
        from omnimarket.nodes.node_emit_daemon.models.model_protocol import (
            ModelDaemonHealthRequest,
            parse_daemon_request,
        )

        req = parse_daemon_request({"command": "health"})
        assert isinstance(req, ModelDaemonHealthRequest)
