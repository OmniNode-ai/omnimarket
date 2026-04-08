# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Async Unix socket server for the emit daemon.

Accepts newline-delimited JSON over Unix domain socket, validates events
via EventRegistry, and enqueues to BoundedEventQueue. Does NOT include
the Kafka publisher loop -- that is a separate component.

Protocol:
    Emit:  {"event_type": "...", "payload": {...}}\\n
    Reply: {"status": "queued", "event_id": "..."}\\n

    Ping:  {"command": "ping"}\\n
    Reply: {"status": "ok", "queue_size": N, "spool_size": N}\\n
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
    EnumCircuitBreakerState,
)
from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_health import (
    ModelEmitDaemonHealth,
)
from omnimarket.nodes.node_emit_daemon.models.model_protocol import (
    JsonType,
    ModelDaemonEmitRequest,
    ModelDaemonErrorResponse,
    ModelDaemonHealthRequest,
    ModelDaemonPingRequest,
    ModelDaemonPingResponse,
    ModelDaemonQueuedResponse,
    parse_daemon_request,
)

logger = logging.getLogger(__name__)


def _json_default(obj: object) -> str:
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class EmitSocketServer:
    """Async Unix domain socket server for event emission.

    Binds to a socket path, accepts newline-delimited JSON, validates
    against the EventRegistry, and enqueues to BoundedEventQueue.

    Args:
        socket_path: Path to the Unix domain socket.
        queue: BoundedEventQueue for accepted events.
        registry: EventRegistry for event validation and fan-out.
        socket_timeout_seconds: Timeout for client read operations.
        socket_permissions: Unix permissions for the socket file.
        max_payload_bytes: Maximum payload size in bytes.
    """

    def __init__(
        self,
        socket_path: str,
        queue: BoundedEventQueue,
        registry: EventRegistry,
        socket_timeout_seconds: float = 5.0,
        socket_permissions: int = 0o660,
        max_payload_bytes: int = 1_048_576,
        publisher_loop: object | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._queue = queue
        self._registry = registry
        self._socket_timeout_seconds = socket_timeout_seconds
        self._socket_permissions = socket_permissions
        self._max_payload_bytes = max_payload_bytes
        # Typed as object to avoid circular import; duck-typed access via attributes.
        self._publisher_loop = publisher_loop

        self._server: asyncio.Server | None = None
        self._shutdown_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        """Bind and start serving on the Unix socket."""
        from pathlib import Path

        socket_path = Path(self._socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)

        stream_limit = self._max_payload_bytes + 4096
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=self._socket_path,
                limit=stream_limit,
            )
        except FileExistsError:
            logger.warning(
                "FileExistsError on first bind attempt; removing socket and retrying"
            )
            socket_path.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=self._socket_path,
                limit=stream_limit,
            )
        socket_path.chmod(self._socket_permissions)
        self._shutdown_event.clear()
        logger.info(f"EmitSocketServer listening on {self._socket_path}")

    async def stop(self) -> None:
        """Stop the socket server."""
        self._shutdown_event.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        from pathlib import Path

        socket_path = Path(self._socket_path)
        if socket_path.exists():
            try:
                socket_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to remove socket file: {e}")

        logger.info("EmitSocketServer stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection (newline-delimited JSON)."""
        try:
            while not self._shutdown_event.is_set():
                try:
                    line = await asyncio.wait_for(
                        reader.readline(),
                        timeout=self._socket_timeout_seconds,
                    )
                except TimeoutError:
                    break

                if not line:
                    break

                response = await self._process_request(line)
                writer.write(response.encode("utf-8") + b"\n")
                await writer.drain()

        except ConnectionResetError:
            pass
        except Exception:
            logger.exception("Error handling client")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                logger.debug("Error closing client writer", exc_info=True)

    async def _process_request(self, line: bytes) -> str:
        try:
            raw_request = json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return ModelDaemonErrorResponse(
                reason=f"Invalid JSON: {e}"
            ).model_dump_json()

        if not isinstance(raw_request, dict):
            return ModelDaemonErrorResponse(
                reason="Request must be a JSON object"
            ).model_dump_json()

        try:
            request = parse_daemon_request(raw_request)
        except (ValueError, ValidationError) as e:
            return ModelDaemonErrorResponse(reason=str(e)).model_dump_json()

        if isinstance(request, ModelDaemonHealthRequest):
            return self._handle_health()
        if isinstance(request, ModelDaemonPingRequest):
            return self._handle_ping()
        return await self._handle_emit(request)

    def _handle_ping(self) -> str:
        return ModelDaemonPingResponse(
            queue_size=self._queue.memory_size(),
            spool_size=self._queue.spool_size(),
        ).model_dump_json()

    def _handle_health(self) -> str:
        """Return detailed health snapshot. Never blocks on I/O."""
        loop = self._publisher_loop
        now = datetime.now(UTC)

        # Extract circuit breaker state from publisher loop if available
        circuit_state = EnumCircuitBreakerState.CLOSED
        consecutive_failures = 0
        events_published = 0
        events_dropped = 0
        events_buffered = 0
        last_publish_at = None
        last_failure_at = None
        circuit_opened_at = None
        kafka_connected = False
        uptime_seconds = 0.0

        if loop is not None:
            circuit_state = getattr(
                loop, "circuit_state", EnumCircuitBreakerState.CLOSED
            )
            consecutive_failures = getattr(loop, "consecutive_failures", 0)
            events_published = getattr(loop, "events_published", 0)
            events_dropped = getattr(loop, "events_dropped", 0)
            events_buffered = getattr(loop, "events_buffered", 0)
            last_publish_at = getattr(loop, "last_publish_at", None)
            last_failure_at = getattr(loop, "last_failure_at", None)
            circuit_opened_at = getattr(loop, "circuit_opened_at", None)
            kafka_connected = getattr(loop, "kafka_connected", False)
            started_at = getattr(loop, "started_at", None)
            if started_at is not None:
                uptime_seconds = (now - started_at).total_seconds()

        healthy = self.is_running and circuit_state != EnumCircuitBreakerState.OPEN

        health = ModelEmitDaemonHealth(
            healthy=healthy,
            circuit_state=circuit_state,
            consecutive_failures=consecutive_failures,
            memory_queue_size=self._queue.memory_size(),
            spool_queue_size=self._queue.spool_size(),
            events_published=events_published,
            events_dropped=events_dropped,
            events_buffered=events_buffered,
            last_publish_at=last_publish_at,
            last_failure_at=last_failure_at,
            circuit_opened_at=circuit_opened_at,
            uptime_seconds=uptime_seconds,
            kafka_connected=kafka_connected,
        )
        return health.model_dump_json()

    def _inject_metadata(
        self,
        payload: dict[str, object],
        correlation_id: str | None,
    ) -> dict[str, object]:
        """Add standard metadata fields to payload."""
        result = dict(payload)
        if "correlation_id" not in result or result["correlation_id"] is None:
            result["correlation_id"] = correlation_id or str(uuid4())
        if "causation_id" not in result:
            result["causation_id"] = None
        if "emitted_at" not in result:
            result["emitted_at"] = datetime.now(UTC).isoformat()
        # Derive entity_id from session_id when absent
        if "entity_id" not in result:
            session_id = result.get("session_id")
            if isinstance(session_id, str) and session_id:
                try:
                    UUID(session_id)
                    result["entity_id"] = session_id
                except ValueError:
                    import hashlib

                    h = hashlib.sha256(session_id.encode()).hexdigest()[:32]
                    result["entity_id"] = str(UUID(h))
        result["schema_version"] = "1.0.0"
        return result

    async def _handle_emit(self, request: ModelDaemonEmitRequest) -> str:
        event_type = request.event_type

        raw_payload = request.payload
        if raw_payload is None:
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            return ModelDaemonErrorResponse(
                reason="'payload' must be a JSON object"
            ).model_dump_json()

        payload: dict[str, object] = raw_payload

        # Look up registration from registry
        registration = self._registry.get_registration(event_type)
        if registration is None:
            return ModelDaemonErrorResponse(
                reason=f"Unknown event type: {event_type}"
            ).model_dump_json()

        # Validate required fields
        try:
            missing = self._registry.validate_payload(event_type, payload)
            if missing:
                return ModelDaemonErrorResponse(
                    reason=f"Missing required fields for {event_type}: {missing}"
                ).model_dump_json()
        except KeyError as e:
            return ModelDaemonErrorResponse(reason=str(e)).model_dump_json()

        # Inject metadata
        correlation_id = payload.get("correlation_id")
        if not isinstance(correlation_id, str):
            correlation_id = None
        enriched_payload = self._inject_metadata(payload, correlation_id)

        # Fan-out: enqueue one event per fan-out rule
        last_event_id: str | None = None

        for rule in registration.fan_out:
            transformed = rule.apply_transform(enriched_payload)
            topic = rule.topic

            # Serialize and check size
            try:
                transformed_json = json.dumps(transformed)
            except (TypeError, ValueError) as e:
                logger.warning(
                    f"Payload serialization failed for {event_type} -> {topic}: {e}"
                )
                continue

            if len(transformed_json.encode("utf-8")) > self._max_payload_bytes:
                logger.warning(
                    f"Payload exceeds max size for {event_type} -> {topic}, skipping"
                )
                continue

            # Get partition key
            try:
                partition_key = self._registry.get_partition_key(
                    event_type, transformed
                )
            except KeyError:
                partition_key = None

            event_id = str(uuid4())
            queued_event = ModelQueuedEvent(
                event_id=event_id,
                event_type=event_type,
                topic=topic,
                payload=cast("JsonType", transformed),
                partition_key=partition_key,
                queued_at=datetime.now(UTC),
            )

            success = await self._queue.enqueue(queued_event)
            if success:
                logger.debug(
                    f"Event queued: {event_id}",
                    extra={"event_type": event_type, "topic": topic},
                )
                last_event_id = event_id
            else:
                logger.warning(f"Failed to queue event for {event_type} -> {topic}")

        if last_event_id is None:
            return ModelDaemonErrorResponse(
                reason=f"Failed to queue any events for {event_type}"
            ).model_dump_json()

        return ModelDaemonQueuedResponse(event_id=last_event_id).model_dump_json()


__all__: list[str] = ["EmitSocketServer"]
