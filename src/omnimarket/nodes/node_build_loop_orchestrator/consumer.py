# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_build_loop_orchestrator.

Subscribes to ``onex.cmd.omnimarket.build-loop-orchestrator-start.v1`` and
runs the live build loop. On completion emits
``onex.evt.omnimarket.build-loop-orchestrator-completed.v1``. On failure emits
a failure event with phase, error, and correlation_id.

Environment:
    KAFKA_BOOTSTRAP_SERVERS  Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER             Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    BUILD_LOOP_GROUP         Consumer group ID
                             (default: local.omnimarket.build-loop-orchestrator.consume.v1)

Usage:
    python -m omnimarket.nodes.node_build_loop_orchestrator.consumer
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

from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    TOPIC_BUILD_LOOP_COMPLETED,
    TOPIC_BUILD_LOOP_FAILED,
    TOPIC_BUILD_LOOP_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.build-loop-orchestrator.consume.v1"


def _parse_command(raw: dict[str, Any]) -> dict[str, Any]:
    correlation_id = raw.get("correlation_id") or str(uuid4())
    max_tickets = int(raw.get("max_tickets", 5))
    max_cycles = int(raw.get("max_cycles", 1))
    dry_run = bool(raw.get("dry_run", False))
    skip_closeout = bool(raw.get("skip_closeout", True))
    return {
        "correlation_id": correlation_id,
        "max_tickets": max_tickets,
        "max_cycles": max_cycles,
        "dry_run": dry_run,
        "skip_closeout": skip_closeout,
    }


async def _invoke_build_loop(cmd: dict[str, Any]) -> dict[str, Any]:
    """Wire and run the live build loop. Returns a completion payload dict."""
    from omnimarket.nodes.node_build_loop_orchestrator.assemble_live import (
        LiveBuildDispatchHandler,
        LiveCloseoutHandler,
        LiveRsdFillHandler,
        LiveTicketClassifyHandler,
        LiveVerifyHandler,
        _build_event_bus,
    )
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )
    from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_start_command import (
        ModelOrchestratorStartCommand,
    )

    correlation_id = UUID(str(cmd["correlation_id"]))
    command = ModelOrchestratorStartCommand(
        correlation_id=correlation_id,
        mode="build",
        max_cycles=cmd["max_cycles"],
        max_tickets=cmd["max_tickets"],
        dry_run=cmd["dry_run"],
        skip_closeout=cmd["skip_closeout"],
        requested_at=datetime.now(tz=UTC),
    )

    orchestrator = HandlerBuildLoopOrchestrator(
        closeout=LiveCloseoutHandler(),
        verify=LiveVerifyHandler(),
        rsd_fill=LiveRsdFillHandler(),
        classify=LiveTicketClassifyHandler(),
        dispatch=LiveBuildDispatchHandler(dry_run_global=cmd["dry_run"]),
        event_bus=_build_event_bus(),
    )

    result = await orchestrator.handle(command)

    pr_refs: list[str] = []
    cost_event_keys: list[str] = []
    for summary in result.cycle_summaries:
        pr_refs.extend(getattr(summary, "pr_refs", []) or [])
        cost_event_keys.extend(getattr(summary, "cost_event_keys", []) or [])

    return {
        "correlation_id": str(correlation_id),
        "cycles_completed": result.cycles_completed,
        "cycles_failed": result.cycles_failed,
        "total_tickets_dispatched": result.total_tickets_dispatched,
        "pr_refs": pr_refs,
        "cost_event_keys": cost_event_keys,
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }


async def _run_consumer(broker: str, group_id: str) -> None:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    except ImportError:
        logger.error("aiokafka is not installed. Install with: uv add aiokafka")
        sys.exit(1)

    consumer = AIOKafkaConsumer(
        TOPIC_BUILD_LOOP_START,
        bootstrap_servers=broker,
        group_id=group_id,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=broker,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
    )

    await consumer.start()
    await producer.start()
    logger.info(
        "build-loop consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_BUILD_LOOP_START,
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
                "received build-loop-start command correlation_id=%s "
                "max_cycles=%d max_tickets=%d dry_run=%s",
                correlation_id,
                cmd["max_cycles"],
                cmd["max_tickets"],
                cmd["dry_run"],
            )

            try:
                payload = await _invoke_build_loop(cmd)
                await producer.send_and_wait(TOPIC_BUILD_LOOP_COMPLETED, payload)
                logger.info(
                    "build-loop-completed emitted correlation_id=%s "
                    "cycles_completed=%d total_dispatched=%d",
                    correlation_id,
                    payload["cycles_completed"],
                    payload["total_tickets_dispatched"],
                )
            except Exception as exc:
                failure: dict[str, Any] = {
                    "correlation_id": correlation_id,
                    "phase": "build_loop",
                    "error": str(exc),
                    "failed_at": datetime.now(tz=UTC).isoformat(),
                }
                await producer.send_and_wait(TOPIC_BUILD_LOOP_FAILED, failure)
                logger.error(
                    "build-loop failed correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("build-loop consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get(
        "KAFKA_BOOTSTRAP_SERVERS",
        os.environ.get("KAFKA_BROKER", "localhost:9092"),
    )
    group_id = os.environ.get("BUILD_LOOP_GROUP", _DEFAULT_GROUP)
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
