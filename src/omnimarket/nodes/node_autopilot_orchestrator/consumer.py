# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_autopilot_orchestrator.

Subscribes to ``onex.cmd.omnimarket.autopilot-orchestrator-start.v1`` and
runs the 4-phase autonomous close-out pipeline. On completion emits
``onex.evt.omnimarket.autopilot-orchestrator-completed.v1``. On failure
emits ``onex.evt.omnimarket.autopilot-orchestrator-failed.v1`` with
correlation_id and error.

Environment:
    KAFKA_BOOTSTRAP_SERVERS    Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER               Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    AUTOPILOT_ORCH_GROUP       Consumer group ID
                               (default: local.omnimarket.autopilot_orchestrator.consume.1.0.0)

Usage:
    python -m omnimarket.nodes.node_autopilot_orchestrator.consumer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
    TOPIC_AUTOPILOT_COMPLETED,
    TOPIC_AUTOPILOT_FAILED,
    TOPIC_AUTOPILOT_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.autopilot_orchestrator.consume.1.0.0"


def _parse_command(raw: dict[str, Any]) -> dict[str, Any]:
    correlation_id = raw.get("correlation_id") or str(uuid4())
    mode = str(raw.get("mode", "close-out"))
    dry_run = bool(raw.get("dry_run", False))
    autonomous = bool(raw.get("autonomous", True))
    return {
        "correlation_id": correlation_id,
        "mode": mode,
        "dry_run": dry_run,
        "autonomous": autonomous,
    }


async def _invoke_autopilot(cmd: dict[str, Any]) -> dict[str, Any]:
    from typing import cast

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

    from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
        HandlerAutopilotOrchestrator,
    )
    from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_start_command import (
        ModelAutopilotStartCommand,
    )

    correlation_id = UUID(str(cmd["correlation_id"]))
    command = ModelAutopilotStartCommand(
        correlation_id=correlation_id,
        mode=cmd["mode"],
        dry_run=cmd["dry_run"],
        autonomous=cmd["autonomous"],
    )

    bus = EventBusInmemory()
    await bus.start()
    orchestrator = HandlerAutopilotOrchestrator(
        event_bus=cast(ProtocolEventBusPublisher, bus)
    )
    result = await orchestrator.handle(command)

    return {
        "correlation_id": str(correlation_id),
        "overall_status": result.overall_status.value
        if hasattr(result.overall_status, "value")
        else str(result.overall_status),
        "halt_reason": result.halt_reason,
        "phases_completed": result.phases_completed,
        "phases_failed": result.phases_failed,
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }


def _build_failure_payload(correlation_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "phase": "autopilot_orchestrator",
        "error": str(exc),
        "failed_at": datetime.now(tz=UTC).isoformat(),
    }


async def _run_consumer(broker: str, group_id: str) -> None:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    except ImportError:
        logger.error("aiokafka is not installed. Install with: uv add aiokafka")
        sys.exit(1)

    consumer = AIOKafkaConsumer(
        TOPIC_AUTOPILOT_START,
        bootstrap_servers=broker,
        group_id=group_id,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=False,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=broker,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
    )

    await consumer.start()
    await producer.start()
    logger.info(
        "autopilot-orchestrator consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_AUTOPILOT_START,
    )

    stop_event = asyncio.Event()

    def _handle_signal(sig: int, _: Any) -> None:
        logger.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break

            raw: dict[str, Any] = msg.value if isinstance(msg.value, dict) else {}
            cmd = _parse_command(raw)
            correlation_id = cmd["correlation_id"]
            logger.info(
                "received autopilot-orchestrator-start command correlation_id=%s "
                "mode=%s dry_run=%s",
                correlation_id,
                cmd["mode"],
                cmd["dry_run"],
            )

            try:
                payload = await _invoke_autopilot(cmd)
                await producer.send_and_wait(TOPIC_AUTOPILOT_COMPLETED, payload)
                await consumer.commit()
                logger.info(
                    "autopilot-orchestrator-completed emitted correlation_id=%s "
                    "overall_status=%s phases_completed=%d phases_failed=%d",
                    correlation_id,
                    payload["overall_status"],
                    payload["phases_completed"],
                    payload["phases_failed"],
                )
            except Exception as exc:
                failure = _build_failure_payload(correlation_id, exc)
                await producer.send_and_wait(TOPIC_AUTOPILOT_FAILED, failure)
                await consumer.commit()
                logger.error(
                    "autopilot-orchestrator failed correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("autopilot-orchestrator consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
        "KAFKA_BROKER", ""
    )
    group_id = os.environ.get("AUTOPILOT_ORCH_GROUP", _DEFAULT_GROUP)
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
