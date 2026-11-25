# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared broker publisher and host-name read for host tick entrypoints (OMN-25093)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.topic_namespace import apply_topic_namespace

NODE = "node_host_job_claim_compute"


def host_name_from_env(variable: str) -> str:
    """The host name a unit's environment declares, or empty when it names none."""
    return os.environ.get(variable, "")


class DeclaredBrokerPublisher:
    """Publish the run record on the broker the node's overlay ``event_bus`` declares (OMN-25093).

    The unit's environment names no broker: the lane id comes from the node's overlay, the address and the client
    transport from the lane declaration table, exactly as the capture silence watch tick publishes (OMN-24342).
    ``KAFKA_BOOTSTRAP_SERVERS`` is never read. Delivery never raises: a refusal returns False and the tick exits 3.

    ``node`` names the node whose overlay declares the broker; it defaults to the host job claim node, the first caller.
    """

    def __init__(self, node: str = NODE) -> None:
        self._node = node

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        try:
            asyncio.run(
                self._send(
                    self._node,
                    topic,
                    event_type,
                    payload,
                    event_id,
                    correlation_id,
                    partition_key,
                )
            )
        except Exception as exc:
            sys.stderr.write(f"PUBLISH REFUSED {type(exc).__name__}: {exc}\n")
            return False
        return True

    @staticmethod
    async def _send(
        node: str,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None,
        partition_key: str | None = None,
    ) -> None:
        from aiokafka import AIOKafkaProducer

        broker = resolve_dialable_broker(node)
        options: dict[str, str] = {}
        if broker.sasl_mechanism is not None:
            options["sasl_mechanism"] = broker.sasl_mechanism
        if broker.sasl_username is not None:
            options["sasl_plain_username"] = broker.sasl_username
        if broker.sasl_password is not None:
            options["sasl_plain_password"] = broker.sasl_password.get_secret_value()
        envelope = {
            "event_type": event_type,
            "event_id": event_id,
            "correlation_id": correlation_id,
            "payload": dict(payload),
        }
        producer = AIOKafkaProducer(
            bootstrap_servers=broker.bootstrap_servers,
            security_protocol=broker.security_protocol or "PLAINTEXT",
            **options,
        )
        await producer.start()
        try:
            value = json.dumps(envelope, sort_keys=True).encode("utf-8")
            if partition_key:
                await producer.send_and_wait(
                    apply_topic_namespace(topic),
                    value,
                    key=partition_key.encode("utf-8"),
                )
            else:
                await producer.send_and_wait(apply_topic_namespace(topic), value)
        finally:
            await producer.stop()


__all__ = ["DeclaredBrokerPublisher", "host_name_from_env"]
