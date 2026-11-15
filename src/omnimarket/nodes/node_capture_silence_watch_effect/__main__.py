# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one capture silence evaluation from the local timer."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Final, Never

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_capture_silence_watch_effect.container.container_capture_silence_watch_effect import (
    build_handler_capture_silence_watch,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    EnumCaptureWatchState,
    ModelCaptureWatchTrigger,
)
from omnimarket.topic_namespace import apply_topic_namespace

NODE_NAME: Final[str] = "node_capture_silence_watch_effect"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def build_handler(
    *, event_publisher: Callable[[str, bytes], Awaitable[None] | None], host: str
) -> HandlerCaptureSilenceWatch:
    """Keep timer assembly overridable by hermetic CLI callers."""
    return build_handler_capture_silence_watch(
        event_publisher=event_publisher, host=host
    )


def _client_kwargs(broker: ModelNodeBroker) -> dict[str, str]:
    """The aiokafka SASL options a declared broker sets, without unset keys."""
    kwargs: dict[str, str] = {}
    if broker.sasl_mechanism is not None:
        kwargs["sasl_mechanism"] = broker.sasl_mechanism
    if broker.sasl_username is not None:
        kwargs["sasl_plain_username"] = broker.sasl_username
    if broker.sasl_password is not None:
        kwargs["sasl_plain_password"] = broker.sasl_password.get_secret_value()
    return kwargs


async def _publish(topic: str, payload: bytes, *, dry_run: bool) -> None:
    """Send each event over the declared broker, or print its topic-addressed JSON to stderr."""
    if dry_run:
        sys.stderr.write(
            json.dumps({"topic": topic, "payload": json.loads(payload)}) + "\n"
        )
        return
    broker = resolve_dialable_broker(NODE_NAME)
    await _publish_kafka(broker, topic, payload)


async def _publish_kafka(broker: ModelNodeBroker, topic: str, payload: bytes) -> None:
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(
        bootstrap_servers=broker.bootstrap_servers,
        security_protocol=broker.security_protocol or "PLAINTEXT",
        **_client_kwargs(broker),
    )
    await producer.start()
    try:
        await producer.send_and_wait(apply_topic_namespace(topic), payload)
    finally:
        await producer.stop()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("operation", choices=["tick"])
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        handler = build_handler(
            event_publisher=lambda topic, payload: _publish(
                topic, payload, dry_run=args.dry_run
            ),
            host=os.environ.get("ONEX_HOST_NAME") or socket.gethostname(),
        )
        result = asyncio.run(handler.handle(ModelCaptureWatchTrigger()))
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    if (
        any(
            verdict.state is EnumCaptureWatchState.UNKNOWN
            for verdict in result.verdicts
        )
        or any(not delivery.published for delivery in result.deliveries)
        or not result.heartbeat_published
    ):
        return 3
    if result.deliveries:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
