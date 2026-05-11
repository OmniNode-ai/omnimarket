# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_pr_lifecycle_orchestrator.

Subscribes to ``onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`` and
runs the merge sweep workflow. On completion emits
``onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1``.

Environment:
    KAFKA_BOOTSTRAP_SERVERS   Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER              Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    PR_LIFECYCLE_GROUP        Consumer group ID
                              (default: local.omnimarket.pr-lifecycle-orchestrator.consume.v1)

Usage:
    python -m omnimarket.nodes.node_pr_lifecycle_orchestrator.consumer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_PR_LIFECYCLE_COMPLETED,
    TOPIC_PR_LIFECYCLE_FAILED,
    TOPIC_PR_LIFECYCLE_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.pr-lifecycle-orchestrator.consume.v1"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _parse_command(raw: dict[str, Any]) -> dict[str, Any]:
    correlation_id = raw.get("correlation_id") or str(uuid4())
    run_id_raw = (
        raw.get("run_id")
        or datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S") + f"-{correlation_id[:6]}"
    )
    # Sanitize run_id to match the pattern required by ModelPrLifecycleStartCommand
    run_id = re.sub(r"[^A-Za-z0-9._-]", "-", str(run_id_raw))[:128]
    if not _RUN_ID_RE.match(run_id):
        run_id = f"run-{correlation_id[:8]}"
    return {
        "correlation_id": correlation_id,
        "run_id": run_id,
        "dry_run": bool(raw.get("dry_run", False)),
        "inventory_only": bool(raw.get("inventory_only", False)),
        "fix_only": bool(raw.get("fix_only", False)),
        "merge_only": bool(raw.get("merge_only", False)),
        "repos": str(raw.get("repos", "")),
        "max_parallel_polish": int(raw.get("max_parallel_polish", 20)),
        "enable_auto_rebase": bool(raw.get("enable_auto_rebase", True)),
        "use_dag_ordering": bool(raw.get("use_dag_ordering", True)),
    }


async def _invoke_pr_lifecycle(cmd: dict[str, Any]) -> dict[str, Any]:
    """Wire and run the PR lifecycle orchestrator. Returns a completion payload."""
    from typing import cast

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
        ModelPrLifecycleStartCommand,
    )

    command = ModelPrLifecycleStartCommand(
        correlation_id=UUID(str(cmd["correlation_id"])),
        run_id=cmd["run_id"],
        dry_run=cmd["dry_run"],
        inventory_only=cmd["inventory_only"],
        fix_only=cmd["fix_only"],
        merge_only=cmd["merge_only"],
        repos=cmd["repos"],
        max_parallel_polish=cmd["max_parallel_polish"],
        enable_auto_rebase=cmd["enable_auto_rebase"],
        use_dag_ordering=cmd["use_dag_ordering"],
    )

    bus = cast(ProtocolEventBusPublisher, EventBusInmemory())
    result = await HandlerPrLifecycleOrchestrator(event_bus=bus).handle(command)

    return {
        "correlation_id": str(cmd["correlation_id"]),
        "run_id": cmd["run_id"],
        "prs_inventoried": result.prs_inventoried,
        "prs_merged": result.prs_merged,
        "prs_fixed": result.prs_fixed,
        "prs_skipped": result.prs_skipped,
        "final_state": result.final_state,
        "error_message": result.error_message,
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }


def _build_failure_payload(cmd: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "correlation_id": str(cmd.get("correlation_id", "")),
        "run_id": str(cmd.get("run_id", "")),
        "phase": "pr_lifecycle",
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
        TOPIC_PR_LIFECYCLE_START,
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
        "pr-lifecycle consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_PR_LIFECYCLE_START,
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
                "received pr-lifecycle-start command correlation_id=%s "
                "run_id=%s dry_run=%s",
                correlation_id,
                cmd["run_id"],
                cmd["dry_run"],
            )

            try:
                payload = await _invoke_pr_lifecycle(cmd)
                await producer.send_and_wait(TOPIC_PR_LIFECYCLE_COMPLETED, payload)
                await consumer.commit()
                logger.info(
                    "pr-lifecycle-completed emitted correlation_id=%s "
                    "prs_merged=%d prs_fixed=%d final_state=%s",
                    correlation_id,
                    payload["prs_merged"],
                    payload["prs_fixed"],
                    payload["final_state"],
                )
            except Exception as exc:
                failure = _build_failure_payload(cmd, exc)
                await producer.send_and_wait(TOPIC_PR_LIFECYCLE_FAILED, failure)
                await consumer.commit()
                logger.error(
                    "pr-lifecycle failed correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("pr-lifecycle consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or os.environ.get(
        "KAFKA_BROKER", ""
    )
    group_id = os.environ.get("PR_LIFECYCLE_GROUP", _DEFAULT_GROUP)
    asyncio.run(_run_consumer(broker, group_id))


if __name__ == "__main__":
    main()
