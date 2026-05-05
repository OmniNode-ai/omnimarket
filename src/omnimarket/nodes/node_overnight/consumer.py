# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_overnight.

Subscribes to ``onex.cmd.omnimarket.overnight-start.v1`` and runs the
overnight session pipeline. On completion emits
``onex.evt.omnimarket.overnight-session-completed.v1``. On failure emits
``onex.evt.omnimarket.overnight-session-failed.v1`` with correlation_id
and error.

Environment:
    KAFKA_BOOTSTRAP_SERVERS   Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER              Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    OVERNIGHT_GROUP           Consumer group ID
                              (default: local.omnimarket.overnight.consume.1.0.0)

Usage:
    python -m omnimarket.nodes.node_overnight.consumer
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

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    TOPIC_OVERNIGHT_COMPLETE as TOPIC_OVERNIGHT_COMPLETED,
)
from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    TOPIC_OVERNIGHT_FAILED,
    TOPIC_OVERNIGHT_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.overnight.consume.1.0.0"


def _parse_command(raw: dict[str, Any]) -> dict[str, Any]:
    correlation_id = raw.get("correlation_id") or str(uuid4())
    max_cycles = int(raw.get("max_cycles", 0))
    skip_nightly_loop = bool(raw.get("skip_nightly_loop", False))
    skip_build_loop = bool(raw.get("skip_build_loop", False))
    skip_merge_sweep = bool(raw.get("skip_merge_sweep", False))
    dry_run = bool(raw.get("dry_run", False))
    enable_self_loop = bool(raw.get("enable_self_loop", True))
    loop_delay_seconds = int(raw.get("loop_delay_seconds", 300))
    return {
        "correlation_id": correlation_id,
        "max_cycles": max_cycles,
        "skip_nightly_loop": skip_nightly_loop,
        "skip_build_loop": skip_build_loop,
        "skip_merge_sweep": skip_merge_sweep,
        "dry_run": dry_run,
        "enable_self_loop": enable_self_loop,
        "loop_delay_seconds": loop_delay_seconds,
    }


def _invoke_overnight(cmd: dict[str, Any]) -> dict[str, Any]:
    from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
        HandlerOvernight,
        ModelOvernightCommand,
    )

    command = ModelOvernightCommand(
        correlation_id=cmd["correlation_id"],
        max_cycles=cmd["max_cycles"],
        skip_nightly_loop=cmd["skip_nightly_loop"],
        skip_build_loop=cmd["skip_build_loop"],
        skip_merge_sweep=cmd["skip_merge_sweep"],
        dry_run=cmd["dry_run"],
        enable_self_loop=cmd["enable_self_loop"],
        loop_delay_seconds=cmd["loop_delay_seconds"],
    )

    handler = HandlerOvernight(event_bus=None, contract_path=None)
    result = handler.handle(command, dispatch_phases=True)

    return {
        "correlation_id": str(cmd["correlation_id"]),
        "session_status": result.session_status.value
        if hasattr(result.session_status, "value")
        else str(result.session_status),
        "phases_run": result.phases_run,
        "phases_failed": result.phases_failed,
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }


def _build_failure_payload(correlation_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "phase": "overnight",
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
        TOPIC_OVERNIGHT_START,
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
        "overnight consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_OVERNIGHT_START,
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
                "received overnight-start command correlation_id=%s "
                "max_cycles=%d dry_run=%s",
                correlation_id,
                cmd["max_cycles"],
                cmd["dry_run"],
            )

            try:
                payload = await asyncio.to_thread(_invoke_overnight, cmd)
                await producer.send_and_wait(TOPIC_OVERNIGHT_COMPLETED, payload)
                await consumer.commit()
                logger.info(
                    "overnight-session-completed emitted correlation_id=%s "
                    "session_status=%s phases_run=%d phases_failed=%d",
                    correlation_id,
                    payload["session_status"],
                    len(payload["phases_run"]),
                    len(payload["phases_failed"]),
                )
            except Exception as exc:
                failure = _build_failure_payload(correlation_id, exc)
                await producer.send_and_wait(TOPIC_OVERNIGHT_FAILED, failure)
                await consumer.commit()
                logger.error(
                    "overnight failed correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("overnight consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get(
        "KAFKA_BOOTSTRAP_SERVERS",
        os.environ.get("KAFKA_BROKER", "localhost:9092"),
    )
    group_id = os.environ.get("OVERNIGHT_GROUP", _DEFAULT_GROUP)
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
