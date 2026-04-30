#!/usr/bin/env python3
"""Proof runner for the Pattern B broker publish-to-terminal flow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
from uuid import uuid4

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pattern_b_broker.handlers import (
    AdapterPatternBBrokerPublish,
    AdapterPatternBBrokerTerminalConsumer,
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerTerminalEvent,
)


async def run_inmemory_proof() -> dict[str, Any]:
    """Run publish -> terminal wait with EventBusInmemory."""
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config()
        publisher = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        terminal_consumer = AdapterPatternBBrokerTerminalConsumer(
            event_bus=bus,
            config=config,
        )
        request = ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            originator="omnimarket",
            recipient="omniclaude",
            skill_name="session-orchestrator",
            payload={"proof": "pattern_b_broker_inmemory"},
        )
        wait_task = asyncio.create_task(
            terminal_consumer.wait_for_terminal_event(
                request,
                timeout_seconds=1.0,
            )
        )

        receipt = await publisher.publish(request)
        await asyncio.sleep(0)
        terminal_event = ModelPatternBBrokerTerminalEvent(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            state=EnumPatternBBrokerState.completed,
            status=EnumPatternBBrokerTerminalStatus.completed,
            result={"proof": "completed"},
        )
        await bus.publish(
            topic=config.topics.terminal_completed_topic,
            key=str(request.request_id).encode("utf-8"),
            value=terminal_event.model_dump_json().encode("utf-8"),
        )
        observed_terminal = await wait_task
        dispatch_history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic,
        )
        terminal_history = await bus.get_event_history(
            topic=config.topics.terminal_completed_topic,
        )
        passed = (
            receipt.request_id == observed_terminal.request_id
            and receipt.correlation_id == observed_terminal.correlation_id
            and observed_terminal.status is EnumPatternBBrokerTerminalStatus.completed
            and len(dispatch_history) == 1
            and len(terminal_history) == 1
        )
        return {
            "mode": "inmemory",
            "status": "passed" if passed else "failed",
            "request_id": str(request.request_id),
            "correlation_id": str(request.correlation_id),
            "dispatch_topic": receipt.topic,
            "terminal_topic": config.topics.terminal_completed_topic,
            "receipt": receipt.model_dump(mode="json"),
            "terminal_event": observed_terminal.model_dump(mode="json"),
            "dispatch_events": len(dispatch_history),
            "terminal_events": len(terminal_history),
        }
    finally:
        await bus.close()


def run_host_live_probe() -> dict[str, Any]:
    """Report the explicit gate for future host-live proof execution."""
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    if not bootstrap:
        return {
            "mode": "host-live",
            "status": "skipped",
            "reason": "KAFKA_BOOTSTRAP_SERVERS is required for host-live broker proof",
        }
    return {
        "mode": "host-live",
        "status": "skipped",
        "reason": (
            "host-live broker process registration is tracked by the follow-up "
            "runtime proof ticket"
        ),
        "bootstrap_configured": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inmemory", "host-live"), default="inmemory")
    args = parser.parse_args(argv)

    if args.mode == "host-live":
        result = run_host_live_probe()
    else:
        result = asyncio.run(run_inmemory_proof())

    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"passed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
