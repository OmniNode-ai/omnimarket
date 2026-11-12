# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosted-bus publisher of node_runtime_bus_forward_effect: one confluent-kafka producer on the declared lane (OMN-24003).

The lane is the one the node's own overlay declares (``event_bus.lane``); its broker and client transport are
resolved as ``onex delegate`` and ``onex scm`` resolve them, from the lane declaration and the host's credential.
Nothing here reads a broker address from the environment or a flag.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Protocol

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols.protocol_runtime_bus_publisher import (
    PublisherRefusedError,
)

_SASL_ENVIRONMENT = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD")


class ProtocolKafkaProducer(Protocol):
    """The two calls of a confluent-kafka producer this publisher makes."""

    def produce(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes | None,
        headers: list[tuple[str, bytes]],
        on_delivery: Callable[[object, object], None],
    ) -> None: ...

    def flush(self, timeout: float) -> int: ...


def broker_for_lane(lane: str, environ: Mapping[str, str]) -> ModelNodeBroker:
    """The broker and client transport of ``lane``, resolved from the lane declaration and the host's credential."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
    from pydantic import SecretStr

    selection, transport = resolve_lane_binding(
        lane,
        environ={
            "OMNIBASE_PATH": environ.get("OMNIBASE_PATH")
            or environ.get("OMNI_HOME", ""),
            **{name: environ.get(name, "") for name in _SASL_ENVIRONMENT},
        },
    )
    if transport is not None:
        return ModelNodeBroker(
            kind="kafka",
            lane=lane,
            bootstrap_servers=transport.bootstrap_servers,
            security_protocol=transport.security_protocol,
            sasl_mechanism=transport.sasl_mechanism,
            sasl_username=transport.sasl_username,
            sasl_password=transport.sasl_password,
        )
    username, password = (environ.get(name, "").strip() for name in _SASL_ENVIRONMENT)
    return ModelNodeBroker(
        kind="kafka",
        lane=lane,
        bootstrap_servers=selection.bootstrap_servers,
        security_protocol=selection.security_protocol,
        sasl_mechanism=selection.sasl_mechanism,
        sasl_username=username or None,
        sasl_password=SecretStr(password) if password else None,
    )


class KafkaRuntimeBusPublisher:
    """Publishes to the hosted bus and counts the events the broker did not confirm."""

    def __init__(self, producer: ProtocolKafkaProducer) -> None:
        self._producer = producer
        self._failures: list[str] = []

    @classmethod
    def from_broker(cls, broker: ModelNodeBroker) -> KafkaRuntimeBusPublisher:
        producer_cls = importlib.import_module("confluent_kafka").Producer
        return cls(
            producer_cls(
                {
                    "bootstrap.servers": broker.bootstrap_servers,
                    **broker.client_security(),
                }
            )
        )

    def _on_delivery(self, err: object, _msg: object) -> None:
        if err is not None:
            self._failures.append(str(err))

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        try:
            self._producer.produce(
                topic,
                value=value,
                key=key,
                headers=[
                    (name, text.encode("utf-8")) for name, text in headers.items()
                ],
                on_delivery=self._on_delivery,
            )
        except (BufferError, ValueError, TypeError, RuntimeError) as exc:
            raise PublisherRefusedError(f"{type(exc).__name__}: {exc}") from exc

    def flush(self, timeout_s: float) -> int:
        remaining = int(self._producer.flush(timeout_s))
        return remaining + len(self._failures)
