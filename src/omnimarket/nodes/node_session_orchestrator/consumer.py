# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_session_orchestrator.

Subscribes to ``onex.cmd.omnimarket.session-orchestrator-start.v1`` and runs
the unified session orchestrator. On completion emits
``onex.evt.omnimarket.session-orchestrator-completed.v1``. On failure emits
``onex.evt.omnimarket.session-orchestrator-failed.v1`` with correlation_id
and error.

Environment:
    KAFKA_BOOTSTRAP_SERVERS   Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER              Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    SESSION_ORCH_GROUP        Consumer group ID
                              (default: local.omnimarket.session_orchestrator.consume.1.0.0)

Usage:
    python -m omnimarket.nodes.node_session_orchestrator.consumer
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
from uuid import uuid4

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    TOPIC_SESSION_ORCH_COMPLETED,
    TOPIC_SESSION_ORCH_FAILED,
    TOPIC_SESSION_ORCH_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.session_orchestrator.consume.1.0.0"


def _parse_command(raw: dict[str, Any]) -> dict[str, Any]:
    correlation_id = raw.get("correlation_id") or str(uuid4())
    session_id = str(raw.get("session_id", ""))
    mode = str(raw.get("mode", "interactive"))
    dry_run = bool(raw.get("dry_run", False))
    skip_health = bool(raw.get("skip_health", False))
    standing_orders_path = str(
        raw.get("standing_orders_path", ".onex_state/session/standing_orders.json")
    )
    state_dir = str(raw.get("state_dir", ".onex_state/session"))
    phase = int(raw.get("phase", 0))
    return {
        "correlation_id": correlation_id,
        "session_id": session_id,
        "mode": mode,
        "dry_run": dry_run,
        "skip_health": skip_health,
        "standing_orders_path": standing_orders_path,
        "state_dir": state_dir,
        "phase": phase,
    }


def _invoke_session_orchestrator(cmd: dict[str, Any]) -> dict[str, Any]:
    from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
        HandlerSessionOrchestrator,
        ModelSessionOrchestratorCommand,
    )

    command = ModelSessionOrchestratorCommand(
        correlation_id=cmd["correlation_id"],
        session_id=cmd["session_id"],
        mode=cmd["mode"],
        dry_run=cmd["dry_run"],
        skip_health=cmd["skip_health"],
        standing_orders_path=cmd["standing_orders_path"],
        state_dir=cmd["state_dir"],
        phase=cmd["phase"],
    )

    handler = HandlerSessionOrchestrator()
    result = handler.handle(command)

    return {
        "correlation_id": str(cmd["correlation_id"]),
        "session_id": result.session_id,
        "status": result.status.value
        if hasattr(result.status, "value")
        else str(result.status),
        "halt_reason": result.halt_reason,
        "dry_run": result.dry_run,
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }


def _build_failure_payload(correlation_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "phase": "session_orchestrator",
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
        TOPIC_SESSION_ORCH_START,
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
        "session-orchestrator consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_SESSION_ORCH_START,
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
                "received session-orchestrator-start command correlation_id=%s "
                "session_id=%s mode=%s dry_run=%s phase=%d",
                correlation_id,
                cmd["session_id"],
                cmd["mode"],
                cmd["dry_run"],
                cmd["phase"],
            )

            try:
                payload = await asyncio.to_thread(_invoke_session_orchestrator, cmd)
                await producer.send_and_wait(TOPIC_SESSION_ORCH_COMPLETED, payload)
                await consumer.commit()
                logger.info(
                    "session-orchestrator-completed emitted correlation_id=%s status=%s",
                    correlation_id,
                    payload["status"],
                )
            except Exception as exc:
                failure = _build_failure_payload(correlation_id, exc)
                await producer.send_and_wait(TOPIC_SESSION_ORCH_FAILED, failure)
                await consumer.commit()
                logger.error(
                    "session-orchestrator failed correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("session-orchestrator consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
        "KAFKA_BROKER", ""
    )
    group_id = os.environ.get("SESSION_ORCH_GROUP", _DEFAULT_GROUP)
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
