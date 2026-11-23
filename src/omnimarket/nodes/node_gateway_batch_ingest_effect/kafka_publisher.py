# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The door's own producer: built with the overlay's `message.max.bytes`, one delivery callback per record.

`KafkaRuntimeBusPublisher` of node_runtime_bus_forward_effect hides its producer, builds it without `message.max.bytes`
and decides every outcome through one list of failures drained by a producer-wide flush, so per-record confirmation
cannot be read from it. The door builds its producer from the same lane-binding reader that wrapper uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Protocol

from pydantic import SecretStr

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelPublishOutcome,
    ModelWireRecord,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)

_SASL_ENVIRONMENT = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD")
_CONFIG_ROOT_ENV = "ONEX_WORKSPACE_CONFIG_ROOT"


class ProtocolProducer(Protocol):
    """The calls of a confluent-kafka producer the door makes."""

    def produce(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes | None,
        headers: list[tuple[str, bytes]],
        on_delivery: Callable[[object, object], None],
    ) -> None: ...

    def poll(self, timeout: float) -> int: ...

    def flush(self, timeout: float) -> int: ...


@contextlib.contextmanager
def _config_root_declared(root: str | None) -> Iterator[None]:
    """Name the overlay's mounted config root for the lane resolver while the lane binds.

    The dev runtime mounts the lane declaration but exports no ONEX_WORKSPACE_CONFIG_ROOT, so the resolver found
    no config/lab/ci_bus_lanes.yaml and the lifespan failed. A root the environment already names wins; the
    variable is put back as it was once the lane is bound, because only the bind reads it.
    """
    if root is None or os.environ.get(_CONFIG_ROOT_ENV, "").strip():
        yield
        return
    os.environ[_CONFIG_ROOT_ENV] = root
    try:
        yield
    finally:
        os.environ.pop(_CONFIG_ROOT_ENV, None)


def broker_for_lane(lane: str, environ: Mapping[str, str]) -> ModelNodeBroker:
    """The broker and client transport of `lane`, resolved from the lane declaration and the host's credential."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding

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


def producer_config(
    broker: ModelNodeBroker, overlay: ModelBatchIngestOverlay
) -> dict[str, str | int]:
    """The librdkafka settings: the broker's security plus the overlay's request limit and message timeout."""
    return {
        "bootstrap.servers": broker.bootstrap_servers or "",
        **broker.client_security(),
        "message.max.bytes": overlay.kafka.producer_max_request_bytes,
        "message.timeout.ms": overlay.kafka.message_timeout_ms,
    }


class KafkaConfirmingPublisher:
    """Publishes one record and decides its outcome from its own delivery callback only."""

    def __init__(
        self,
        producer: ProtocolProducer,
        *,
        message_max_bytes: int,
        confirm_timeout_s: float,
        poll_interval_s: float = 0.005,
    ) -> None:
        self._producer = producer
        self._message_max_bytes = message_max_bytes
        self._confirm_timeout_s = confirm_timeout_s
        self._poll_interval_s = poll_interval_s

    @classmethod
    def from_overlay(
        cls,
        overlay: ModelBatchIngestOverlay,
        environ: Mapping[str, str],
        *,
        broker: ModelNodeBroker | None = None,
    ) -> KafkaConfirmingPublisher:
        if broker is None:
            with _config_root_declared(overlay.kafka.workspace_config_root):
                broker = broker_for_lane(overlay.kafka.lane, environ)
        resolved = broker
        producer_cls = importlib.import_module("confluent_kafka").Producer
        return cls(
            producer_cls(producer_config(resolved, overlay)),
            message_max_bytes=overlay.kafka.producer_max_request_bytes,
            confirm_timeout_s=overlay.kafka.message_timeout_ms / 1000 + 1.0,
        )

    @property
    def message_max_bytes(self) -> int:
        return self._message_max_bytes

    async def publish(self, record: ModelWireRecord) -> ModelPublishOutcome:
        loop = asyncio.get_running_loop()
        settled: asyncio.Future[object] = loop.create_future()

        def settle(error: object) -> None:
            if not settled.done():
                settled.set_result(error)

        def on_delivery(error: object, _message: object) -> None:
            try:
                loop.call_soon_threadsafe(settle, error)
            except RuntimeError:
                return  # the loop is closed: nobody is waiting for this record any more

        try:
            self._producer.produce(
                record.topic,
                value=record.value,
                key=record.key,
                headers=list(record.headers),
                on_delivery=on_delivery,
            )
        except (BufferError, ValueError, TypeError, RuntimeError) as exc:
            return ModelPublishOutcome(
                status="publish-refused", detail=type(exc).__name__
            )
        deadline = loop.time() + self._confirm_timeout_s
        while not settled.done():
            self._producer.poll(0)
            if settled.done():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                return ModelPublishOutcome(
                    status="delivery-unconfirmed", detail="no delivery callback"
                )
            await asyncio.wait({settled}, timeout=min(self._poll_interval_s, remaining))
        error = settled.result()
        if error is not None:
            return ModelPublishOutcome(status="publish-refused", detail=str(error))
        return ModelPublishOutcome(status="published")

    def close(self) -> None:
        self._producer.flush(5.0)


__all__ = [
    "KafkaConfirmingPublisher",
    "ProtocolProducer",
    "broker_for_lane",
    "producer_config",
]
